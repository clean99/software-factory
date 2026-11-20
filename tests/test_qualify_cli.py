"""bin/factory qualify accepts an issue reference or a repository with collision keywords."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from factory import cli
from factory.oss import qualify


class QualifyCommand(unittest.TestCase):
    def setUp(self):
        policy = patch("factory.oss.policy.run", return_value={"flags": {}})
        self.policy = policy.start()
        self.addCleanup(policy.stop)

    def test_automation_restriction_blocks_with_quoted_policy(self):
        quote = "CONTRIBUTING.md:3: AI contributions require prior permission."
        self.policy.return_value = {"flags": {"ai_restriction": [quote]}}
        with patch.object(qualify, "run_keywords", return_value={"verdict": "ok", "skip": []}):
            code, text = self.call("o/r", "--keyword", "parser")
        self.assertEqual(code, 2)
        self.assertIn(quote, json.loads(text)["skip"])

    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            try:
                code = cli.main(["qualify", *args])
            except SystemExit as error:
                code = error.code
        return code, output.getvalue()

    def test_repository_with_keywords_runs_the_keyword_collision_check(self):
        result = {"verdict": "ok", "repo": "o/r", "issue": None, "target": "none", "skip": [], "warn": []}
        with patch.object(qualify, "run_keywords", return_value=result) as keywords:
            code, text = self.call("o/r", "--keyword", "ExactSymbol", "--keyword", "other phrase")
        self.assertEqual(code, 0)
        keywords.assert_called_once_with("o/r", ["ExactSymbol", "other phrase"])
        self.assertEqual(json.loads(text)["target"], "none")

    def test_repository_collision_exits_two(self):
        result = {"verdict": "skip", "repo": "o/r", "issue": None, "target": "none", "skip": ["open PR matches"], "warn": []}
        with patch.object(qualify, "run_keywords", return_value=result):
            code, _ = self.call("o/r", "--keyword", "ExactSymbol")
        self.assertEqual(code, 2)

    def test_repository_without_keywords_says_what_to_add(self):
        with patch.object(qualify, "run_keywords", side_effect=AssertionError("no lookup without keywords")):
            code, _ = self.call("o/r")
        self.assertIn("--keyword", str(code))

    def test_issue_reference_still_uses_the_issue_check(self):
        result = {"verdict": "ok", "repo": "o/r", "issue": 7, "target": "issue"}
        with patch.object(qualify, "run", return_value=result) as run:
            code, _ = self.call("o/r#7", "--keyword", "phrase")
        self.assertEqual(code, 0)
        run.assert_called_once_with("o/r", 7, ["phrase"], "issue")

    def qualify_issue(self, body="", comments=(), reporter="reporter", days=2):
        issue = {"number": 7, "title": "Reproducible bug", "body": body, "state": "open", "labels": [],
                 "assignees": [], "user": {"login": reporter}, "author_association": "NONE",
                 "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-01T00:00:00Z",
                 "html_url": "https://github.com/o/r/issues/7", "comments": len(comments)}
        timeline = [{"event": "commented", "body": text, "user": {"login": author},
                     "author_association": association, "created_at": issue["created_at"]}
                    for author, association, text in comments]

        def fake_gh(*args):
            if args == ("api", "repos/o/r/issues/7"):
                return issue
            if args[:2] == ("pr", "list"):
                return []
            self.fail(f"Unexpected lookup: {args}")

        with patch.object(qualify, "gh_json", side_effect=fake_gh), \
                patch.object(qualify, "gh_paginate", return_value=timeline) as paginate, \
                patch.object(qualify, "hours_since", return_value=days * 24), \
                patch.dict(qualify.CONFIG, {"github_user": "factory-user"}):
            code, text = self.call("o/r#7")
        paginate.assert_called_once_with("repos/o/r/issues/7/timeline")
        return code, json.loads(text)

    def test_reporter_contribution_intent_blocks_body_and_comments_with_quote(self):
        sentences = ("I'd like to submit a PR.", "I can open a PR.", "happy to work on this!",
                     "I'll send a fix.", "I am willing to submit a PR.", "I’d like to open a pull request.")
        for sentence in sentences:
            for source in ("body", "comment"):
                with self.subTest(sentence=sentence, source=source):
                    code, result = self.qualify_issue(
                        body="Description of the bug. " + sentence if source == "body" else "",
                        comments=[("reporter", "NONE", sentence)] if source == "comment" else [], days=60)
                    self.assertEqual(code, 2)
                    self.assertEqual(result["verdict"], "skip")
                    self.assertIn(repr(sentence), "\n".join(result["skip"]))
                    self.assertIn(source, "\n".join(result["skip"]))

    def test_checked_willingness_option_blocks_but_unchecked_does_not(self):
        for mark in ("x", "X", " "):
            for source in ("body", "comment"):
                with self.subTest(mark=mark, source=source):
                    sentence = "I am willing to submit a PR."
                    body = f"### Contribution\n- [{mark}] {sentence}"
                    code, result = self.qualify_issue(body=body if source == "body" else "",
                        comments=[("reporter", "NONE", body)] if source == "comment" else [])
                    self.assertEqual(code, 0 if mark == " " else 2)
                    if mark != " ":
                        self.assertIn(repr(sentence), result["skip"][0])

    def test_invitations_quotes_examples_and_negations_are_not_claims(self):
        bodies = ("I am not willing to submit a PR.", "Can someone open a PR?",
                  "> I can open a PR.\nThat was the old report.",
                  "```text\nI'll send a fix.\n```", 'The template says "I can open a PR".',
                  "If you are happy to work on this, PRs are welcome.")
        for body in bodies:
            with self.subTest(body=body):
                code, result = self.qualify_issue(body=body, comments=[
                    ("maintainer", "MEMBER", "PRs welcome. Feel free to open a PR."),
                    ("maintainer", "MEMBER", "If you'd like to submit a PR, that would be welcome.")])
                self.assertEqual(code, 0, result)
                self.assertEqual(result["skip"], [])
        code, result = self.qualify_issue(body="I can open a PR.", reporter="factory-user",
                                        comments=[("factory-user", "NONE", "I'll send a fix.")])
        self.assertEqual(code, 0, result)

    def test_maintainer_personal_commitment_stays_blocking_when_old(self):
        code, result = self.qualify_issue(comments=[("maintainer", "MEMBER", "I'll fix this.")], days=60)
        self.assertEqual(code, 2, result)
        self.assertIn(repr("I'll fix this."), result["skip"][0])

    def test_other_contributor_claims_keep_the_existing_staleness_window(self):
        for days in (2, 60):
            with self.subTest(days=days):
                code, result = self.qualify_issue(comments=[("helper", "NONE", "I'll send a fix.")], days=days)
                self.assertEqual(code, 2 if days == 2 else 0)
                self.assertIn(repr("I'll send a fix."), "\n".join(result["skip"] + result["warn"]))

    def test_malformed_reference_is_refused_with_the_accepted_forms(self):
        with patch.object(qualify, "run", side_effect=AssertionError("no lookup")):
            code, _ = self.call("not a reference", "--keyword", "phrase")
        self.assertIn("owner/repo", str(code))


if __name__ == "__main__":
    unittest.main()
