"""Owner-delegated account operations check what makes each write safe before running it."""
import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from support import setup_module
from factory import cli
from factory.core import ownerops
from factory.util import CommandError

ME = "fixture-owner"
NOTE = "owner, 2026-11-11: clean these up"


def setUpModule():
    setup_module(unittest)


class OwnerOperations(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.log = pathlib.Path(temporary.name) / "ledger" / "owner-ops.jsonl"
        self.writes = []
        self.reads = {}
        for manager in (patch.object(ownerops, "LOG", self.log),
                        patch.object(ownerops, "run", side_effect=lambda command, **_: self.writes.append(command)),
                        patch.object(ownerops, "gh_json", side_effect=self.read)):
            manager.__enter__()
            self.addCleanup(manager.__exit__, None, None, None)

    def read(self, *args, **_):
        for key, value in self.reads.items():
            if key in " ".join(args):
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected read: {args}")

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def fork(self, branches=("main",), open_prs=0, ahead=None, unsent=()):
        self.unsent = set(unsent)
        self.reads = {
            "search/issues": {"total_count": open_prs},
            "/branches": [{"name": name} for name in branches],
            "repos/up/project/compare": None,
            f"repos/{ME}/project": {"fork": True, "parent": {"full_name": "up/project", "default_branch": "main"}},
        }
        self.ahead = ahead or {}

        def read(*args, **_):
            joined = " ".join(args)
            if "/pulls?head=" in joined:
                branch = unquote(joined.split(":", 1)[1].split("&", 1)[0])
                return [] if branch in self.unsent else [{"number": 1, "state": "closed"}]
            if "/compare/" in joined:
                branch = joined.rsplit(":", 1)[1]
                value = self.ahead.get(branch, 0)
                if isinstance(value, Exception):
                    raise value
                return {"ahead_by": value}
            return self.read(*args)

        ownerops.gh_json.side_effect = read

    def test_closes_our_own_open_pull_request_and_records_it(self):
        url = "https://github.com/up/project/pull/7"
        self.reads = {"pr view": {"author": {"login": "Fixture-Owner"}, "state": "OPEN", "title": "Old try"}}
        self.assertEqual(ownerops.close_pr(url, NOTE), {"closed": url, "title": "Old try"})
        self.assertEqual(self.writes, [["gh", "pr", "close", "7", "--repo", "up/project"]])
        record = self.records()[0]
        self.assertEqual((record["action"], record["target"], record["note"]), ("close-pr", url, NOTE))

    def test_refuses_another_authors_or_a_finished_pull_request(self):
        url = "https://github.com/up/project/pull/7"
        for pr, expected in (({"author": {"login": "someone"}, "state": "OPEN"}, "our own"),
                             ({"author": {"login": ME}, "state": "MERGED"}, "merged"),
                             ({"author": {"login": ME}, "state": "CLOSED"}, "closed"),
                             (None, "our own")):
            with self.subTest(pr=pr):
                self.reads = {"pr view": pr}
                with self.assertRaisesRegex(CommandError, expected):
                    ownerops.close_pr(url, NOTE)
        with self.assertRaisesRegex(CommandError, "pull request URL"):
            ownerops.close_pr("https://github.com/up/project/issues/7", NOTE)
        self.assertEqual((self.writes, self.records()), ([], []))

    def test_describes_only_our_own_project(self):
        self.reads = {"repo view": {"isFork": False}}
        ownerops.repo_about(f"{ME}/tool", "Checks one thing well.", ["testing", "git-worktrees"], NOTE)
        self.assertEqual(self.writes, [["gh", "repo", "edit", f"{ME}/tool", "--description", "Checks one thing well.",
                                        "--add-topic", "testing", "--add-topic", "git-worktrees"]])
        self.assertEqual(self.records()[0]["topics"], ["testing", "git-worktrees"])
        self.writes.clear()
        for repo, info, description, topics, expected in (
                ("up/project", {"isFork": False}, "Text.", [], "our own account"),
                (f"{ME}/fork", {"isFork": True}, "Text.", [], "fork"),
                (f"{ME}/tool", {}, "Text.", [], "fork"),
                (f"{ME}/tool", {"isFork": False}, None, [], "--description"),
                (f"{ME}/tool", {"isFork": False}, "Two\nlines", [], "one line"),
                (f"{ME}/tool", {"isFork": False}, "x" * 351, [], "one line"),
                (f"{ME}/tool", {"isFork": False}, "Text.", ["Bad Topic"], "topics")):
            with self.subTest(repo=repo, description=description, topics=topics):
                self.reads = {"repo view": info}
                with self.assertRaisesRegex(CommandError, expected):
                    ownerops.repo_about(repo, description, topics, NOTE)
        self.assertEqual(self.writes, [])

    def test_description_must_pass_the_publication_scan(self):
        self.reads = {"repo view": {"isFork": False}}
        with patch.object(ownerops.leaks, "scan", return_value=["description: private term"]):
            with self.assertRaisesRegex(CommandError, "publication scan"):
                ownerops.repo_about(f"{ME}/tool", "Mentions something private.", [], NOTE)
        self.assertEqual(self.writes, [])

    def test_deletes_a_fork_whose_branches_are_merged_or_factory_branches(self):
        self.fork(branches=("main", "contrib/fix-one", "old-sync"), ahead={"contrib/fix-one": 3})
        self.assertEqual(ownerops.delete_fork(f"{ME}/project", NOTE), {"deleted": f"{ME}/project"})
        self.assertEqual(self.writes, [["gh", "repo", "delete", f"{ME}/project", "--yes"]])
        self.assertEqual(self.records()[0]["action"], "delete-fork")

    def test_keeps_a_fork_with_an_open_pull_request_or_the_owners_own_commits(self):
        cases = (
            (dict(open_prs=1), "open pull request"),
            (dict(branches=("main", "my-app"), ahead={"my-app": 12}), "branch my-app holds 12 commit"),
            (dict(branches=("main",), ahead={"main": CommandError("404")}), "could not be compared"),
            (dict(branches=("main", "contrib/kept-proposal"), unsent=("contrib/kept-proposal",)), "never became a pull request"),
        )
        for setup, expected in cases:
            with self.subTest(setup=setup):
                self.fork(**setup)
                with self.assertRaisesRegex(CommandError, expected):
                    ownerops.delete_fork(f"{ME}/project", NOTE)
        self.fork()
        self.reads["search/issues"] = {}
        with self.assertRaisesRegex(CommandError, "unknown number"):
            ownerops.delete_fork(f"{ME}/project", NOTE)
        self.assertEqual((self.writes, self.records()), ([], []))

    def test_never_deletes_another_account_or_one_of_our_own_projects(self):
        self.assertIn("our own account", ownerops.fork_reasons("up/project")[0])
        self.reads = {f"repos/{ME}/tool": {"fork": False}}
        self.assertIn("not a fork", ownerops.fork_reasons(f"{ME}/tool")[0])
        self.reads = {f"repos/{ME}/tool": {"fork": True}}
        self.assertIn("not a fork", ownerops.fork_reasons(f"{ME}/tool")[0])
        self.assertEqual(self.writes, [])

    def test_branch_listing_follows_pages(self):
        pages = {1: [{"name": f"contrib/b{index}"} for index in range(ownerops.PAGE)], 2: [{"name": "main"}]}
        ownerops.gh_json.side_effect = lambda *args, **_: pages[int(args[1].rsplit("=", 1)[1])]
        self.assertEqual(len(ownerops._branches(f"{ME}/project")), ownerops.PAGE + 1)

    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            try:
                code = cli.main(["owner", *args])
            except SystemExit as error:
                return error.code, output.getvalue()
        return code, output.getvalue()

    def test_command_line_dry_run_reports_and_real_run_keeps_refused_forks(self):
        self.fork(branches=("main", "my-app"), ahead={"my-app": 2})
        code, text = self.call("delete-fork", f"{ME}/project", "--dry-run", "--note", NOTE)
        report = json.loads(text)
        self.assertEqual((code, report[0]["deletable"]), (0, False))
        self.assertIn("my-app", report[0]["why"][0])
        code, text = self.call("delete-fork", f"{ME}/project", "--note", NOTE)
        self.assertEqual((code, json.loads(text)[0]["kept"]), (0, f"{ME}/project"))
        self.assertEqual(self.writes, [])
        code, _ = self.call("close-pr", "https://github.com/up/project/pull/7")
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
