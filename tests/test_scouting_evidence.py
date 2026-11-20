"""Qualification evidence from issue comments, assignments and linked work."""
import json
import pathlib
import unittest
from unittest.mock import patch

from factory.oss import qualify
from factory.util import CommandError


class QualificationEvidence(unittest.TestCase):
    def issue(self):
        return {"number": 7, "title": "Reproducible bug", "body": "", "state": "open", "labels": [],
                "assignees": [], "user": {"login": "reporter"}, "author_association": "NONE",
                "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-01T00:00:00Z",
                "html_url": "https://github.com/o/r/issues/7", "comments": 0}

    def comment(self, text, association="NONE", author="helper"):
        return {"event": "commented", "body": text, "user": {"login": author},
                "author_association": association, "created_at": "2026-11-01T00:00:00Z"}

    def pr(self, state="MERGED", body="Fixes #7", repo="o/r", number=8):
        return {"__typename": "PullRequest", "number": number, "state": state, "merged": state == "MERGED", "title": "Repair ordering",
                "body": body, "author": {"login": "helper"}, "createdAt": "2026-11-01T00:00:00Z",
                "url": f"https://github.com/{repo}/pull/{number}"}

    def check(self, issue=None, timeline=(), linked=(), search=(), repo="o/r", number=7):
        def gh(*args):
            if args == ("api", f"repos/{repo}/issues/{number}"):
                return issue or self.issue()
            if args[:2] == ("api", "graphql"):
                self.assertTrue(linked, args)
                self.assertEqual(args[-1].count("{"), args[-1].count("}"), args[-1])
                field = "issueOrPullRequest" if "issueOrPullRequest(" in args[-1] else "pullRequest"
                if field == "pullRequest" and any(pr and pr.get("__typename") == "Issue" for pr in linked):
                    # gh exits nonzero even when other aliases contain valid PR data.
                    raise CommandError('gh api graphql failed (1): gh: Could not resolve to a PullRequest with the number of 7.')
                return {"data": {f"p{i}": {field: {"__typename": "PullRequest", **pr} if pr else None}
                                 for i, pr in enumerate(linked)}}
            if args[:2] == ("pr", "list"):
                return search
            self.fail(f"Unexpected lookup: {args}")

        with patch.object(qualify, "gh_json", side_effect=gh) as lookup, \
                patch.object(qualify, "gh_paginate", return_value=list(timeline)), \
                patch.object(qualify, "hours_since", return_value=60 * 24), \
                patch.dict(qualify.CONFIG, {"github_user": "factory-user"}):
            result = qualify.run(repo, number)
        self.assertLessEqual(sum(call.args[:2] == ("api", "graphql") for call in lookup.call_args_list), 1)
        return result

    def blocked(self, result, quote):
        self.assertEqual(result["verdict"], "skip", result)
        self.assertIn(repr(quote), "\n".join(result["skip"]))

    def test_other_assignees_block_even_when_owner_is_also_assigned(self):
        for names in (("helper",), ("factory-user", "helper")):
            issue = self.issue()
            issue["assignees"] = [{"login": name} for name in names]
            self.blocked(self.check(issue), "helper")
        issue["assignees"] = [{"login": "FACTORY-USER"}]
        self.assertEqual(self.check(issue)["verdict"], "ok")

    def test_work_claims_block_with_full_quote_even_when_old(self):
        for text in ("I'm working on this.", "I’m working on a fix.", "I am working on this.",
                     "I'll take this.", "PR incoming", "Working on a fix.",
                     "I’m coordinating there; once it ships I’ll follow up here with a regression test."):
            with self.subTest(text=text):
                self.blocked(self.check(timeline=[self.comment(text)]), text)

    def test_prefixed_work_claims_and_maintainer_assertions_keep_full_quote(self):
        cases = [("Hi, I'm working on this.", "NONE"), ("Hey, I'll take this.", "NONE"),
                 ("Update: working on a fix.", "NONE"), ("Hello all, PR incoming.", "NONE"),
                 ("Thanks for reporting, this is fixed in v2.0.", "MEMBER"),
                 ("Quick update: this has been released.", "MEMBER"),
                 ("Hi, fixed in 2.0.", "MEMBER"), ("FYI, we'll handle this.", "MEMBER")]
        for text, association in cases:
            with self.subTest(text=text):
                self.blocked(self.check(timeline=[self.comment(text, association)]), text)

    def test_prefixed_questions_negations_and_examples_are_not_assertions(self):
        for text in ("Hi, is anyone working on this?", "Update: working on a fix?", "Hey, PRs welcome.",
                     "Thanks for reporting, this is not fixed in main.", "Update: I'm not working on this.",
                     "Thanks, this has not been released.", 'Example: "I am working on this."',
                     "If needed, I'll take this.", 'Hi, the template says "fixed in 2.0".',
                     "> Hi, I'm working on this.", "```text\nUpdate: working on a fix.\n```",
                     "- [ ] Hi, I'll take this."):
            for association in ("NONE", "MEMBER"):
                with self.subTest(text=text, association=association):
                    result = self.check(timeline=[self.comment(text, association)])
                    self.assertEqual(result["verdict"], "ok", result)

    def test_mixed_pr_and_issue_references_do_not_abort_the_batch(self):
        for text in ("Related PR #8; issue #7 tracks the report.", "See PR #8 for issue #7.",
                     "See https://github.com/o/r/pull/8 for https://github.com/o/r/issues/7."):
            with self.subTest(text=text):
                issue = self.issue()
                issue["body"] = text
                issue_node = {"__typename": "Issue"}
                result = self.check(issue, linked=[self.pr("OPEN"), issue_node])
                self.blocked(result, "Fixes #7")
                result = self.check(issue, linked=[self.pr("OPEN", body="Unrelated change"), issue_node])
                self.assertEqual(result["verdict"], "ok", result)

    def test_genuine_link_lookup_failures_remain_incomplete(self):
        for error in (CommandError("gh api graphql failed (1): authentication failed"),
                      CommandError("gh api graphql failed (1): network unavailable")):
            with self.subTest(error=str(error)), patch.object(qualify, "gh_json", side_effect=error):
                with self.assertRaises(CommandError):
                    qualify.linked_fixes("o/r", 7, ["Related PR #8"])
        response = {"data": {"p0": {"issueOrPullRequest": None}},
                    "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to an IssueOrPullRequest"}]}
        with patch.object(qualify, "gh_json", return_value=response):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                qualify.linked_fixes("o/r", 7, ["Related PR #8"])

    def test_maintainer_fixed_and_released_statements(self):
        for text in ("This is already fixed on main.", "Fixed in 2.0.", "This has been released.",
                     "The fix is shipped.", "Released in 1.2.3."):
            with self.subTest(text=text):
                self.blocked(self.check(timeline=[self.comment(text, "MEMBER")]), text)

    def test_questions_invites_negations_and_quoted_claims_do_not_block(self):
        for text in ("Is anyone working on this?", "PRs welcome", "Feel free to open a PR.",
                     "This is not fixed in main.", "This has not been released.", "Is this fixed in 2.0?",
                     "Working on a fix?", "I'm not working on this.", "> I'm working on this.",
                     "```text\nPR incoming\n```", "<!-- I'm working on this. -->", "- [ ] I'll take this."):
            for association in ("NONE", "MEMBER"):
                with self.subTest(text=text, association=association):
                    result = self.check(timeline=[self.comment(text, association)])
                    self.assertEqual(result["verdict"], "ok", result)

    def test_linked_fix_fetches_once_and_only_blocks_open_or_merged_prs(self):
        quote = "This is already fixed on main by #8."
        for state in ("OPEN", "MERGED", "CLOSED"):
            with self.subTest(state=state):
                result = self.check(timeline=[self.comment(quote, "CONTRIBUTOR")], linked=[self.pr(state)])
                if state != "CLOSED":
                    self.blocked(result, quote)
                else:
                    self.assertEqual(result["verdict"], "ok", result)
        result = self.check(timeline=[self.comment(quote)], linked=[None])
        self.assertEqual(result["verdict"], "ok", result)

    def test_fix_evidence_supersedes_an_earlier_bare_link(self):
        issue = self.issue()
        issue["body"] = "Related PR: https://github.com/o/r/pull/8"
        quote = "This is already fixed on main by #8."
        pr = self.pr(body="Predates the issue; no closing keyword")
        pr["author"]["login"] = "factory-user"
        self.blocked(self.check(issue, timeline=[self.comment(quote)], linked=[pr]), quote)

    def test_cross_referenced_closing_pr_quotes_exact_body(self):
        for state in ("open", "closed"):
            for verb in ("Fixes", "Closes", "Resolves"):
                quote = f"{verb} o/r#7"
                event = {"event": "cross-referenced", "source": {"issue": {
                    "number": 8, "html_url": "https://github.com/o/r/pull/8", "state": state,
                    "user": {"login": "helper"}, "body": quote, "title": "Repair ordering",
                    "repository": {"full_name": "o/r"},
                    "pull_request": {"merged_at": "2026-11-01T00:00:00Z" if state == "closed" else None}}}}
                with self.subTest(state=state, verb=verb):
                    self.blocked(self.check(timeline=[event]), quote)

    def test_cross_repository_bare_number_is_not_a_closing_reference(self):
        event = {"event": "cross-referenced", "source": {"issue": {
            "number": 8, "html_url": "https://github.com/other/project/pull/8", "state": "open",
            "user": {"login": "helper"}, "body": "Fixes #7", "title": "Different issue",
            "repository": {"full_name": "other/project"}, "pull_request": {"merged_at": None}}}}
        self.assertEqual(self.check(timeline=[event])["verdict"], "ok")
        event["source"]["issue"]["body"] = "Fixes https://github.com/o/r/issues/7"
        self.blocked(self.check(timeline=[event]), "Fixes https://github.com/o/r/issues/7")

    def test_linked_pr_body_and_github_closing_references_are_checked(self):
        issue = self.issue()
        issue["body"] = "Related PR: https://github.com/o/r/pull/8"
        self.blocked(self.check(issue, linked=[self.pr("OPEN")]), "Fixes #7")
        pr = self.pr("OPEN", body="Repair ordering")
        self.assertEqual(self.check(issue, linked=[pr])["verdict"], "ok")
        pr["closingIssuesReferences"] = {"nodes": [{"number": 7, "repository": {"nameWithOwner": "o/r"}}]}
        self.blocked(self.check(issue, linked=[pr]), "GitHub closing reference: o/r#7")
        pr["closingIssuesReferences"]["nodes"][0]["repository"]["nameWithOwner"] = "other/repo"
        self.assertEqual(self.check(issue, linked=[pr])["verdict"], "ok")

    def test_search_and_development_sidebar_keep_quoted_evidence(self):
        self.blocked(self.check(search=[self.pr("OPEN")]), "Fixes #7")
        self.blocked(self.check(timeline=[{"event": "connected"}]), "connected")

    def test_synthetic_issue_and_claim_shapes(self):
        fixture = pathlib.Path(__file__).with_name("fixtures") / "scouting-evidence.json"
        for case in json.loads(fixture.read_text()):
            with self.subTest(repo=case["repo"]):
                issue = {**self.issue(), **case["issue"]}
                result = self.check(issue=issue, timeline=case["timeline"], linked=case.get("linked", []),
                                    repo=case["repo"], number=issue["number"])
                self.blocked(result, case["quote"])


if __name__ == "__main__":
    unittest.main()
