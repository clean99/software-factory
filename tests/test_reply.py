import hashlib
import io
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import setup_module

from factory import cli  # noqa: E402
from factory.util import CommandError, read_json, write_json  # noqa: E402


def setUpModule():
    setup_module(unittest)


class ReplyGuard(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = pathlib.Path(temporary.name)
        self.meta = {"repo": "o/r", "pr_url": "https://github.com/o/r/pull/1", "pushed_head": "a" * 40}
        write_json(self.directory / "item.json", self.meta)
        reply = self.directory / "reply.md"
        reply.write_text("Thanks for the review.\n")
        write_json(self.directory / "approval-reply.json", {"sha256": hashlib.sha256(reply.read_bytes()).hexdigest()})
        self.pr = {"state": "OPEN", "headRefOid": "a" * 40, "comments": []}
        self.out, self.err = io.StringIO(), io.StringIO()
        for mocker in (patch.object(cli, "gh_json", return_value=self.pr),
                       patch.object(cli, "run", return_value=Mock(stdout="https://github.com/o/r/pull/1#issuecomment-2\n"))):
            mock = mocker.start()
            self.addCleanup(mocker.stop)
            if mocker.attribute == "gh_json":
                self.fetch = mock
            else:
                self.post = mock

    def reply(self, force=False):
        with redirect_stdout(self.out), redirect_stderr(self.err):
            return cli.main(["reply", str(self.directory)] + (["--force"] if force else []))

    def test_open_matching_head_reads_immediately_before_post(self):
        calls = []
        self.fetch.side_effect = lambda *a, **kw: calls.append("read") or self.pr
        self.post.side_effect = lambda *a, **kw: calls.append("post") or Mock(stdout="comment-url")
        self.assertEqual(self.reply(), 0)
        self.assertEqual(calls, ["read", "post"])
        self.fetch.assert_called_once_with("pr", "view", self.meta["pr_url"], "--json", "comments,state,headRefOid", timeout=120)
        self.assertEqual(read_json(self.directory / "item.json")["replies"][0]["url"], "comment-url")

    def test_withdrawn_reply_is_never_reposted_even_with_force(self):
        sha = hashlib.sha256((self.directory / "reply.md").read_bytes()).hexdigest()
        for record in ({"file": "reply.md"}, {"file": "older.md", "sha256": sha}):
            with self.subTest(record=record):
                write_json(self.directory / "item.json", {**self.meta, "replies": [
                    {**record, "url": "https://github.com/o/r/pull/1#issuecomment-1", "at": "2026-11-25T02:37:00Z",
                     "withdrawn_at": "2026-11-25T02:44:00Z", "withdrawn_reason": "owner: no CI-status replies"}]})
                for force in (False, True):
                    with self.assertRaisesRegex(SystemExit, "withdrawn .* do not repost"):
                        self.reply(force=force)
        self.post.assert_not_called()

    def test_closed_or_merged_pr_is_refused_without_mutation(self):
        before = (self.directory / "item.json").read_bytes()
        for state in ("CLOSED", "MERGED"):
            self.pr["state"] = state
            with self.subTest(state=state), self.assertRaisesRegex(SystemExit, f"PR is {state}"):
                self.reply()
        self.post.assert_not_called()
        self.assertEqual((self.directory / "item.json").read_bytes(), before)

    def test_changed_head_is_refused_and_both_shas_are_shown(self):
        self.pr["headRefOid"] = "b" * 40
        with self.assertRaises(SystemExit) as error:
            self.reply()
        self.assertIn("a" * 40, str(error.exception))
        self.assertIn("b" * 40, str(error.exception))
        self.assertIn("--force", str(error.exception))
        self.post.assert_not_called()

    def test_force_overrides_both_changes_but_prints_them(self):
        self.pr.update(state="MERGED", headRefOid="b" * 40)
        self.assertEqual(self.reply(force=True), 0)
        self.post.assert_called_once()
        self.assertIn("PR is MERGED", self.err.getvalue())
        self.assertIn("head changed", self.err.getvalue())

    def test_missing_recorded_head_fails_closed(self):
        del self.meta["pushed_head"]
        write_json(self.directory / "item.json", self.meta)
        with self.assertRaisesRegex(SystemExit, "no recorded"):
            self.reply()
        self.post.assert_not_called()
        self.assertEqual(self.reply(force=True), 0)

    def test_legacy_head_sha_is_supported_but_pushed_head_takes_precedence(self):
        self.meta.update(head_sha=self.meta.pop("pushed_head"))
        write_json(self.directory / "item.json", self.meta)
        self.assertEqual(self.reply(), 0)
        self.meta["pushed_head"] = "c" * 40
        write_json(self.directory / "item.json", self.meta)
        with self.assertRaisesRegex(SystemExit, "head changed"):
            self.reply()
        self.assertEqual(self.post.call_count, 1)

    def test_force_does_not_bypass_approval(self):
        (self.directory / "reply.md").write_text("Different reply.\n")
        with self.assertRaisesRegex(SystemExit, "not approved"):
            self.reply(force=True)
        self.fetch.assert_not_called()
        self.post.assert_not_called()

    def test_force_does_not_post_if_live_fetch_fails(self):
        self.fetch.side_effect = CommandError("read timed out after 120s")
        self.assertEqual(self.reply(force=True), 3)
        self.post.assert_not_called()
        self.assertIn("timed out", self.err.getvalue())

    def test_force_still_dedupes_and_handles_deleted_authors(self):
        self.pr.update(state="MERGED", comments=[{"author": None, "body": "Thanks for the review.", "url": "old"},
                                                {"author": {"login": cli.CONFIG["github_user"]}, "body": "Thanks for the review.", "url": "existing"}])
        self.assertEqual(self.reply(force=True), 0)
        self.post.assert_not_called()
        self.assertIn("existing", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()
