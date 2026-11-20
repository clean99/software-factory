import datetime as dt
import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import setup_module

from factory import cli  # noqa: E402
from factory.oss import watch  # noqa: E402
from factory.util import CONFIG, CommandError, iso, read_json, write_json  # noqa: E402

AT = dt.datetime(2026, 10, 28, 12, tzinfo=dt.timezone.utc)
RECENT = "2026-10-28T11:30:00Z"
OLD = "2026-10-20T00:00:00Z"


def node(number, title="fix: adapter input", author="contributor", labels=(), issue=False, **extra):
    result = {"number": number, "title": title, "url": f"https://github.com/o/r/{'issues' if issue else 'pull'}/{number}",
              "author": {"login": author}, "authorAssociation": "CONTRIBUTOR",
              "labels": {"nodes": [{"name": label} for label in labels]}, "mergedAt": RECENT, "baseRefName": "main"}
    if issue:
        result.update(body="", state="OPEN", createdAt=RECENT, updatedAt=RECENT,
                      assignees={"totalCount": 0}, timelineItems={"nodes": []})
    return {**result, **extra}


def connection(nodes):
    return {"nodes": nodes, "issueCount": len(nodes), "pageInfo": {"hasNextPage": False}}


def comment(number, body="PRs welcome", association="MEMBER", created=RECENT, author="maintainer"):
    return {"number": number, "body": body, "authorAssociation": association, "createdAt": created, "author": {"login": author},
            "url": f"https://github.com/o/r/issues/{number}#issuecomment-1"}


def setUpModule():
    setup_module(unittest)


class Watch(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        (self.root / "lines/oss").mkdir(parents=True)
        (self.root / "ledger").mkdir()
        self.runs = self.root / "runs"
        self.state = self.root / "ledger/watch-state.json"
        self.watchlist = [{"repo": "o/r", "tier": 1, "notes": "adapters"}, {"repo": "o/s", "tier": 2, "notes": "plugins"}]
        write_json(self.root / "lines/oss/watch.json", self.watchlist)
        self.merged, self.own, self.issues, self.comments, self.files = [], [], [], [], {}
        self.search = None
        self.tree_entries = [{"name": name, "type": "blob"} for name in ("a.ts", "b.ts", "c.ts")]
        for mocker in (patch.object(watch, "ROOT", self.root), patch.object(watch, "now", return_value=AT),
                       patch.dict(CONFIG, {"runs_dir": str(self.runs), "ledger_file": str(self.root / "ledger/prs.json")}),
                       patch.object(watch, "gh_json", side_effect=self.respond)):
            mock = mocker.start()
            self.addCleanup(mocker.stop)
        self.gh = mock

    def respond(self, *args, **kwargs):
        self.assertEqual(kwargs, {"timeout": 120})
        self.assertEqual(args[0], "api")
        self.assertNotIn("--paginate", args)
        self.assertEqual(args[1], "graphql")
        values = dict(arg.split("=", 1) for arg in args if "=" in arg)
        query = values["query"]
        self.assertNotIn("mutation", query)
        if "search(" in query:
            issues = []
            for issue in self.issues:
                comments = [c for c in self.comments if c["number"] == issue["number"]]
                issues.append({"references": {"nodes": [], "pageInfo": {"hasPreviousPage": False}}, **issue,
                               "comments": {"nodes": comments[-100:], "pageInfo": {"hasPreviousPage": len(comments) > 100}}})
            own = [{"comments": {"nodes": [], "pageInfo": {"hasPreviousPage": False}},
                    "reviews": {"nodes": [], "pageInfo": {"hasPreviousPage": False}}, **pr} for pr in self.own]
            return {"data": self.search or {"merged": connection(self.merged), "issues": connection(issues), "own": connection(own)}}
        if "pullRequest(" in query:
            return {"data": {"repository": {f'p{n["number"]}': {"files": {"nodes": [{"path": f} for f in self.files.get(n["number"], ["src/adapters/a.ts"])],
                                                                        "pageInfo": {"hasNextPage": False}}} for n in self.merged}}}
        return {"data": {"repository": {key: {"entries": self.tree_entries} for key in values if key.startswith("d")}}}

    def test_first_run_looks_back_48_hours_and_writes_report(self):
        report = watch.run(repos=["o/r"])
        self.assertEqual(report["repos"][0]["since"], iso(AT - dt.timedelta(hours=48)))
        self.assertEqual(read_json(self.state), {"o/r": iso(AT)})
        local = AT.astimezone()
        output = self.runs / local.strftime("%Y-%m-%d/watch/%H%M.json")
        self.assertEqual(read_json(output), report)
        self.assertNotIn(str(self.root), json.dumps(report))
        query = " ".join(self.gh.call_args_list[0].args)
        for qualifier in ("is:pr is:merged merged:>=2026-10-26T12:00:00Z", "-author:fixture-owner", "no:assignee", "-linked:pr"):
            self.assertIn(qualifier, query)

    def test_success_uses_and_advances_existing_cursor(self):
        write_json(self.state, {"o/r": RECENT, "o/s": OLD})
        report = watch.run(repos=["O/R"])
        self.assertEqual(report["repos"][0]["since"], iso(watch.timestamp(RECENT)))
        self.assertEqual(read_json(self.state), {"o/r": iso(AT), "o/s": OLD})

    def test_one_failure_keeps_its_cursor_and_does_not_block_other_repo(self):
        write_json(self.state, {"o/r": OLD})
        respond = self.respond

        def fail_one(*args, **kwargs):
            if any("repo:o/r" in arg for arg in args):
                raise CommandError("read timed out after 120s")
            return respond(*args, **kwargs)

        self.gh.side_effect = fail_one
        report = watch.run()
        self.assertEqual([row["status"] for row in report["repos"]], ["error", "ok"])
        self.assertEqual(read_json(self.state), {"o/r": OLD, "o/s": iso(AT)})
        self.assertIn("ERROR read timed out", watch.summary(report)[0])

    def test_dry_run_never_creates_or_changes_state(self):
        report = watch.run(dry_run=True)
        self.assertFalse(self.state.exists())
        self.assertTrue(report["dry_run"])
        write_json(self.state, {"o/r": OLD})
        before = self.state.read_bytes()
        watch.run(dry_run=True)
        self.assertEqual(self.state.read_bytes(), before)

    def test_override_does_not_rewind_a_newer_saved_cursor(self):
        report = watch.run(repos=["o/r"], since=RECENT, dry_run=True)
        self.assertEqual(report["repos"][0]["since"], iso(watch.timestamp(RECENT)))
        future = iso(AT + dt.timedelta(hours=1))
        write_json(self.state, {"o/r": future})
        watch.run(repos=["o/r"], since=OLD)
        self.assertEqual(read_json(self.state)["o/r"], future)

    def test_bad_filters_fail_before_any_api_calls(self):
        for kwargs in ({"since": "yesterday"}, {"since": "2026-10-28"}, {"since": iso(AT)}, {"repos": ["o/not-watched"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                watch.run(**kwargs)
        self.gh.assert_not_called()

    def test_own_and_known_items_are_excluded_recursively(self):
        item = self.runs / "old/deep/nested/item.json"
        item.parent.mkdir(parents=True)
        write_json(item, {"repo": "O/R", "issue": 2, "pr_url": "https://github.com/o/r/pull/3",
                          "related": ["o/r#4", {"repo": "o/r", "number": 5}],
                          "notes": "see #6; https://github.com/other/repo/issues/20"})
        write_json(self.root / "ledger/prs.json", [{"repo": "o/r", "number": 7, "issue": "8", "url": "https://github.com/o/r/pull/9"}])
        self.merged = [node(i, author="FIXTURE-OWNER" if i == 1 else "contributor") for i in range(1, 11)]
        self.issues = [node(2, issue=True), node(11, issue=True, author="fixture-owner")]
        self.comments = [comment(2), comment(11)]
        report = watch.run(repos=["o/r"])
        self.assertEqual([n["number"] for n in report["candidates"]], [10])
        self.assertNotIn(("o/r", 20), watch.known_items())
        files_query = self.gh.call_args_list[1].args[3]
        self.assertIn("p10:pullRequest", files_query)
        self.assertNotIn("p2:pullRequest", files_query)

    def test_fix_filter_only_fetches_files_for_fixes(self):
        self.merged = [node(1), node(2, title="feat: new adapter"), node(3, title="handle input", labels=["regression"]),
                       node(4, title="fix(parser): keep null"), node(5, title="fixture updates")]
        report = watch.run(repos=["o/r"])
        self.assertEqual({n["number"] for n in report["candidates"]}, {1, 3, 4})
        self.assertEqual(len(report["repos"][0]["merged_prs"]), 5)
        self.assertEqual(self.gh.call_count, 3)
        files_query = self.gh.call_args_list[1].args[3]
        self.assertNotIn("p2:pullRequest", files_query)
        self.assertNotIn("p5:pullRequest", files_query)

    def test_ranking_invite_then_bug_repro_then_sibling_fix(self):
        self.merged = [node(1), node(2)]
        self.files[2] = ["README.md"]
        self.issues = [node(3, issue=True, labels=["bug"], createdAt=OLD,
                            timelineItems={"nodes": [{"createdAt": RECENT}]}, body="Repro: https://stackblitz.com/edit/example"),
                       node(4, issue=True, createdAt=OLD), node(5, issue=True, labels=["help wanted"])]
        self.comments = [comment(4)]
        report = watch.run(repos=["o/r"])
        self.assertEqual([n["number"] for n in report["candidates"]], [4, 3, 1, 5, 2])
        self.assertEqual(report["candidates"][2]["siblings"][0]["siblings"], ["src/adapters/b.ts", "src/adapters/c.ts"])

    def test_reopened_and_new_issues_but_not_merely_updated(self):
        self.issues = [node(1, issue=True), node(2, issue=True, createdAt=OLD),
                       node(3, issue=True, createdAt=OLD, timelineItems={"nodes": [{"createdAt": RECENT}]}),
                       node(4, issue=True, state="CLOSED"), node(5, issue=True, assignees={"totalCount": 1})]
        report = watch.run(repos=["o/r"])
        self.assertEqual({n["number"] for n in report["candidates"]}, {1, 3})

    def test_invites_require_new_maintainer_comment_on_available_issue(self):
        self.issues = [node(i, issue=True, createdAt=OLD) for i in range(1, 9)]
        self.comments = [comment(1), comment(2, association="OWNER", body="happy to accept"),
                         comment(3, association="COLLABORATOR", body="Feel free to open a PR"),
                         comment(4, association="CONTRIBUTOR"), comment(5, created=OLD),
                         comment(6, author="fixture-owner"), comment(7, body="thanks"), comment(10)]
        report = watch.run(repos=["o/r"])
        self.assertEqual({n["number"] for n in report["candidates"]}, {1, 2, 3})
        self.assertTrue(all(n["kind"] == "invite" for n in report["candidates"]))

    def test_invitation_phrase_variants(self):
        for phrase in ("PR welcome", "PRs welcome", "PRs are welcome", "PRs are also welcome", "happy to accept", "contributions welcome", "feel free to open a PR"):
            self.assertIsNotNone(watch.INVITE.search(phrase))

    def test_search_overflow_keeps_cursor(self):
        self.search = {"merged": connection([]), "issues": connection([]), "own": connection([])}
        self.search["merged"]["pageInfo"]["hasNextPage"] = True
        report = watch.run(repos=["o/r"])
        self.assertEqual(report["repos"][0]["status"], "error")
        self.assertFalse(self.state.exists())
        self.assertEqual(self.gh.call_count, 1)

    def test_comment_overflow_keeps_cursor(self):
        self.issues = [node(1, issue=True)]
        self.comments = [comment(1)] * 101
        report = watch.run(repos=["o/r"])
        self.assertIn("100-result budget", report["repos"][0]["error"])
        self.assertFalse(self.state.exists())

    def test_old_comment_history_does_not_count_as_delta_overflow(self):
        self.issues = [node(1, issue=True, createdAt=OLD)]
        self.comments = [comment(1, created=OLD)] * 110 + [comment(1)]
        report = watch.run(repos=["o/r"])
        self.assertEqual(report["repos"][0]["status"], "ok")
        self.assertEqual(report["candidates"][0]["kind"], "invite")

    def test_events_after_scan_start_wait_until_next_scan(self):
        future = iso(AT + dt.timedelta(minutes=1))
        self.merged = [node(1, mergedAt=future)]
        self.issues = [node(2, issue=True, createdAt=future), node(3, issue=True, createdAt=OLD)]
        self.comments = [comment(3, created=future)]
        report = watch.run(repos=["o/r"])
        self.assertEqual(report["candidates"], [])
        self.assertEqual(read_json(self.state)["o/r"], iso(AT))

    def test_file_overflow_keeps_cursor(self):
        self.merged = [node(1)]
        respond = self.respond

        def truncate_files(*args, **kwargs):
            data = respond(*args, **kwargs)
            if any("pullRequest(" in arg for arg in args):
                data["data"]["repository"]["p1"]["files"]["pageInfo"]["hasNextPage"] = True
            return data

        self.gh.side_effect = truncate_files
        report = watch.run(repos=["o/r"])
        self.assertIn("100-file budget", report["repos"][0]["error"])
        self.assertFalse(self.state.exists())

    def test_report_write_failure_does_not_advance_cursor(self):
        with patch.object(watch, "write_json", side_effect=OSError("disk full")), self.assertRaises(OSError):
            watch.run(repos=["o/r"])
        self.assertFalse(self.state.exists())

    def test_partial_graphql_response_keeps_cursor(self):
        self.gh.return_value = {"data": {"merged": connection([])}, "errors": [{"message": "failure"}]}
        self.gh.side_effect = None
        report = watch.run(repos=["o/r"])
        self.assertEqual(report["repos"][0]["status"], "error")
        self.assertFalse(self.state.exists())

    def test_failure_fetching_files_does_not_advance(self):
        self.merged = [node(1)]
        respond = self.respond

        def fail_files(*args, **kwargs):
            if any("pullRequest(" in arg for arg in args):
                raise CommandError("file read failed")
            return respond(*args, **kwargs)

        self.gh.side_effect = fail_files
        report = watch.run(repos=["o/r"])
        self.assertEqual(report["repos"][0]["error"], "file read failed")
        self.assertFalse(self.state.exists())

    def test_similar_names_need_several_real_siblings(self):
        trees = {"src": [{"name": name, "type": "blob"} for name in ("parse-a.ts", "parse-b.ts", "parse-c.ts")]}
        self.assertTrue(watch.siblings(["src/parse-a.ts"], trees))
        self.assertFalse(watch.siblings(["src/unrelated.ts"], trees))
        self.assertFalse(watch.siblings(["src/parse-a.ts"], {"src": trees["src"][:2]}))

    def test_directory_lookup_is_bounded_without_dropping_changed_paths(self):
        self.merged = [node(1)]
        self.files[1] = [f"src/part{i}/one.ts" for i in range(40)]
        report = watch.run(repos=["o/r"])
        row = report["repos"][0]
        self.assertEqual(row["sibling_directories_checked"], 30)
        self.assertEqual(row["sibling_directories_omitted"], 10)
        self.assertEqual(len(row["candidates"][0]["files"]), 40)
        self.assertEqual(self.gh.call_count, 3)

    def test_fresh_bug_requires_a_maintainer_invitation(self):
        self.issues = [node(i, issue=True, labels=["bug"]) for i in range(1, 7)]
        self.comments = [comment(2), comment(3, association="CONTRIBUTOR"), comment(4, author="fixture-owner"),
                         comment(5, body="Thanks for the report"), comment(6, created=iso(AT + dt.timedelta(minutes=1)))]
        report = watch.run(repos=["o/r"])
        self.assertEqual([item["number"] for item in report["candidates"]], [2])
        self.assertEqual({item["number"] for item in report["rejected"]}, {1, 3, 4, 5, 6})
        self.assertTrue(all(item["reason"] == "younger than 48h, no maintainer invitation" for item in report["rejected"]))

    def test_bug_issue_type_uses_the_same_freshness_rule(self):
        self.issues = [node(1, issue=True, issueType={"name": "Bug"})]
        report = watch.run(repos=["o/r"])
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["rejected"][0]["number"], 1)

    def test_exactly_48_hours_is_eligible_and_ages_in_without_an_update(self):
        self.issues = [node(1, issue=True, labels=["bug"], createdAt=iso(AT - dt.timedelta(hours=48)), updatedAt=OLD),
                       node(2, issue=True, labels=["bug"], createdAt=iso(AT - dt.timedelta(hours=48) + dt.timedelta(seconds=1)))]
        report = watch.run(repos=["o/r"])
        self.assertEqual([item["number"] for item in report["candidates"]], [1])
        self.assertEqual([item["number"] for item in report["rejected"]], [2])
        with patch.object(watch, "now", return_value=AT + dt.timedelta(days=1)):
            report = watch.run(repos=["o/r"])
        self.assertEqual([item["number"] for item in report["candidates"]], [2])
        query = " ".join(self.gh.call_args_list[-1].args)
        self.assertIn("updated:>=2026-10-26T12:00:00Z", query)

    def test_earlier_invitation_exempts_a_reopened_fresh_bug(self):
        created = iso(AT - dt.timedelta(hours=4))
        self.issues = [node(1, issue=True, labels=["bug"], createdAt=created,
                            timelineItems={"nodes": [{"createdAt": RECENT}]})]
        self.comments = [comment(1, created=iso(AT - dt.timedelta(hours=3)))]
        report = watch.run(repos=["o/r"], since=iso(AT - dt.timedelta(hours=1)))
        self.assertEqual([item["number"] for item in report["candidates"]], [1])
        self.assertEqual(report["rejected"], [])

    def test_own_followups_include_known_merged_pr_comments_reviews_and_inline_comments(self):
        write_json(self.root / "ledger/prs.json", [{"repo": "o/r", "number": 1, "url": "https://github.com/o/r/pull/1"}])
        follow = comment(1, body="Thanks! A follow-up should cover empty input.")
        review = {**comment(1, body="Could you also handle missing paths in another PR?"), "submittedAt": RECENT,
                  "comments": {"nodes": [comment(1, body="This edge case would also benefit from the fix.")],
                               "pageInfo": {"hasPreviousPage": False}}}
        self.own = [node(1, author="FIXTURE-OWNER", mergedAt=OLD,
                         comments={"nodes": [follow], "pageInfo": {"hasPreviousPage": False}},
                         reviews={"nodes": [review], "pageInfo": {"hasPreviousPage": False}})]
        report = watch.run(repos=["o/r"])
        self.assertEqual(len(report["candidates"]), 3)
        self.assertTrue(all(item["kind"] == "own-followup" for item in report["candidates"]))
        self.assertEqual({item["quote"] for item in report["candidates"]},
                         {follow["body"], review["body"], review["comments"]["nodes"][0]["body"]})
        self.assertEqual(self.gh.call_count, 1)
        query = " ".join(self.gh.call_args.args)
        self.assertIn("author:fixture-owner updated:>=2026-10-26T12:00:00Z", query)

    def test_own_followups_require_maintainer_window_and_our_merged_pr(self):
        comments = [comment(1, body="A follow-up", association="CONTRIBUTOR"),
                    comment(1, body="A follow-up", author="fixture-owner"),
                    comment(1, body="A follow-up", created=OLD), comment(1, body="Thanks!"),
                    comment(1, body="A follow-up", created=iso(AT + dt.timedelta(minutes=1)))]
        thread = {"nodes": comments, "pageInfo": {"hasPreviousPage": False}}
        self.own = [node(1, author="fixture-owner", mergedAt=OLD, comments=thread),
                    node(2, author="outsider", comments={"nodes": [comment(2, body="Another PR")], "pageInfo": {"hasPreviousPage": False}}),
                    node(3, author="fixture-owner", mergedAt=iso(AT + dt.timedelta(minutes=1)),
                         comments={"nodes": [comment(3, body="Another PR")], "pageInfo": {"hasPreviousPage": False}})]
        report = watch.run(repos=["o/r"])
        self.assertEqual(report["candidates"], [])

    def test_own_followup_phrase_variants(self):
        for phrase in ("follow-up", "follow up", "would also", "another PR", "edge case", "feel free to", "could you also"):
            with self.subTest(phrase=phrase):
                self.assertIsNotNone(watch.FOLLOWUP.search(phrase))

    def test_tracking_items_are_unchecked_lines_from_maintainers(self):
        body = "Plan\n- [ ] First adapter\n- [x] Finished adapter\n  - [ ] Second adapter\n```md\n- [ ] Example only\n```"
        self.issues = [node(1, issue=True, title="Tracking issue: adapters", authorAssociation="OWNER", body=body),
                       node(2, issue=True, title="Features", authorAssociation="COLLABORATOR", labels=["roadmap"], body="- [ ] Third adapter"),
                       node(3, issue=True, title="Tracking issue", body="- [ ] Not a maintainer"),
                       node(4, issue=True, title="Ordinary issue", authorAssociation="MEMBER", body="- [ ] Not tracking")]
        report = watch.run(repos=["o/r"])
        tracking = [item for item in report["candidates"] if item["kind"] == "tracking-item"]
        self.assertEqual([item["quote"] for item in tracking], ["- [ ] First adapter", "  - [ ] Second adapter", "- [ ] Third adapter"])
        self.assertEqual([item["line"] for item in tracking], [2, 4, 1])
        self.assertTrue(all(item["source"] == item["url"] for item in tracking))

    def test_updated_tracking_issue_is_seen_but_older_body_waits(self):
        self.issues = [node(1, issue=True, title="Roadmap", authorAssociation="MEMBER", body="- [ ] Adapter", createdAt=OLD),
                       node(2, issue=True, title="Roadmap", authorAssociation="MEMBER", body="- [ ] Old", createdAt=OLD, updatedAt=OLD)]
        report = watch.run(repos=["o/r"])
        self.assertEqual([item["number"] for item in report["candidates"]], [1])

    def test_tracking_and_invites_reject_open_prs_assignees_and_recent_claims(self):
        self.issues = [node(i, issue=True, title="Tracking", authorAssociation="MEMBER", body="- [ ] Adapter") for i in range(1, 5)]
        self.issues[0]["references"] = {"nodes": [{"source": {"state": "OPEN", "url": "https://github.com/o/r/pull/20"}}],
                                         "pageInfo": {"hasPreviousPage": False}}
        self.issues[1]["assignees"]["totalCount"] = 1
        self.issues[3]["references"] = {"nodes": [{"source": {"state": "MERGED", "url": "https://github.com/o/r/pull/21"}}],
                                         "pageInfo": {"hasPreviousPage": False}}
        self.comments = [comment(1), comment(2), comment(3), comment(3, body="I'm working on this", association="CONTRIBUTOR")]
        report = watch.run(repos=["o/r"])
        self.assertEqual([item["number"] for item in report["candidates"]], [4])

    def test_fresh_tracking_bug_is_rejected_without_invitation(self):
        self.issues = [node(1, issue=True, title="Tracking bugs", authorAssociation="OWNER", labels=["bug"], body="- [ ] Adapter")]
        report = watch.run(repos=["o/r"])
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["rejected"][0]["kind"], "tracking-item")

    def test_aging_tracking_bug_keeps_its_unchecked_items(self):
        self.issues = [node(1, issue=True, title="Roadmap", authorAssociation="MEMBER", labels=["bug"],
                            body="- [ ] Adapter", createdAt=iso(AT - dt.timedelta(hours=48)),
                            updatedAt=iso(AT - dt.timedelta(hours=48)))]
        report = watch.run(repos=["o/r"], since=iso(AT - dt.timedelta(hours=1)))
        self.assertEqual([item["kind"] for item in report["candidates"]], ["tracking-item"])
        self.assertEqual(report["candidates"][0]["quote"], "- [ ] Adapter")

    def test_new_kinds_rank_first_and_all_candidates_have_evidence(self):
        self.own = [node(6, author="fixture-owner", comments={"nodes": [comment(6, body="Feel free to add a follow-up")],
                                                            "pageInfo": {"hasPreviousPage": False}})]
        self.issues = [node(1, issue=True), node(2, issue=True, title="Roadmap", authorAssociation="MEMBER", body="- [ ] Adapter"),
                       node(3, issue=True, labels=["bug"], createdAt=OLD, timelineItems={"nodes": [{"createdAt": RECENT}]})]
        self.comments = [comment(1)]
        self.merged = [node(4)]
        report = watch.run(repos=["o/r"])
        self.assertEqual([item["kind"] for item in report["candidates"]], ["own-followup", "invite", "tracking-item", "sibling-fix", "issue"])
        for item in report["candidates"]:
            self.assertTrue(item["source"].startswith("https://github.com/"))
            self.assertTrue(item["quote"])
            self.assertTrue(item["url"].startswith("https://github.com/"))
            self.assertEqual(item["observed_at"], iso(AT))
        self.assertEqual(self.gh.call_count, 3)

    def test_own_search_and_thread_overflow_keep_cursor(self):
        for field in ("search", "comments", "reviews", "inline"):
            with self.subTest(field=field):
                self.own = [node(1, author="fixture-owner")]
                self.search = None
                if field == "search":
                    self.search = {"merged": connection([]), "issues": connection([]), "own": connection([])}
                    self.search["own"]["pageInfo"]["hasNextPage"] = True
                else:
                    thread = {"nodes": [comment(1, body="A follow-up")], "pageInfo": {"hasPreviousPage": True}}
                    if field == "inline":
                        self.own[0]["reviews"] = {"nodes": [{**comment(1), "submittedAt": RECENT, "comments": thread}],
                                                  "pageInfo": {"hasPreviousPage": False}}
                    else:
                        self.own[0][field] = thread
                report = watch.run(repos=["o/r"])
                self.assertEqual(report["repos"][0]["status"], "error")
                self.assertFalse(self.state.exists())

    def test_reference_overflow_keeps_cursor(self):
        self.issues = [node(1, issue=True, references={"nodes": [], "pageInfo": {"hasPreviousPage": True}})]
        report = watch.run(repos=["o/r"])
        self.assertEqual(report["repos"][0]["status"], "error")
        self.assertFalse(self.state.exists())

    def test_default_watchlist_comes_from_depth_order(self):
        for content in ([], {}, {"repos": []}):
            with self.subTest(content=content), patch.object(watch.lanes, "order", return_value=["o/s", "o/r"]):
                write_json(self.root / "lines/oss/watch.json", content)
                report = watch.run(dry_run=True)
                self.assertEqual([row["repo"] for row in report["repos"]], ["o/s", "o/r"])
        (self.root / "lines/oss/watch.json").unlink()
        with patch.object(watch.lanes, "order", return_value=["o/s"]):
            self.assertEqual([row["repo"] for row in watch.run(dry_run=True)["repos"]], ["o/s"])

    def test_explicit_watchlist_overrides_lanes(self):
        with patch.object(watch.lanes, "order", side_effect=AssertionError("explicit repos must win")):
            report = watch.run(dry_run=True)
        self.assertEqual([row["repo"] for row in report["repos"]], ["o/r", "o/s"])

    def test_cli_repeatable_repo_since_dry_run_and_summary(self):
        self.issues = [node(i, issue=True) for i in range(20)]
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["watch", "--repo", "o/r", "--repo", "o/s", "--since", OLD, "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(self.state.exists())
        self.assertIn("o/r: 0 sibling-fix, 20 issue, 0 invite", out.getvalue())
        self.assertIn("o/s: 0 sibling-fix, 20 issue, 0 invite", out.getvalue())
        self.assertEqual(sum(line.startswith("issue ") for line in out.getvalue().splitlines()), 10)
        self.assertIn("cursors unchanged", out.getvalue())

    def test_cli_failure_is_visible_and_nonzero(self):
        self.gh.side_effect = CommandError("unavailable")
        with redirect_stdout(io.StringIO()) as out:
            code = cli.main(["watch", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("ERROR unavailable", out.getvalue())


if __name__ == "__main__":
    unittest.main()
