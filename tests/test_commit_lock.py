import datetime as dt
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import setup_module

from factory import cli  # noqa: E402
from factory.core import commit, leaks, publication, shiftlock  # noqa: E402
from factory.util import CONFIG, read_json  # noqa: E402

AT = dt.datetime(2026, 11, 4, 9, tzinfo=dt.timezone.utc)
# No global or system config: the machine's global hooks and identity stay out of the fixture repo.
HERMETIC = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
LEAK = [{"RuleID": "gitlab-pat", "File": "notes.md", "StartLine": 2, "Secret": "REDACTED", "Match": "REDACTED"}]


def setUpModule():
    setup_module(unittest)


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def main(*argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(argv))
    return code, json.loads(out.getvalue())


class ShiftLock(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.clock = AT
        for mocker in (patch.object(shiftlock, "ROOT", self.root), patch.object(shiftlock, "now", side_effect=lambda: self.clock)):
            mocker.start()
            self.addCleanup(mocker.stop)

    def test_concurrent_acquires_have_one_winner(self):
        real = shiftlock.read_json

        def slow_read(path):
            state = real(path)
            time.sleep(0.3)
            return state

        results = {}
        threads = [threading.Thread(target=lambda o=o: results.update({o: shiftlock.run("acquire", o)})) for o in ("morning", "evening")]
        with patch.object(shiftlock, "read_json", side_effect=slow_read):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        winners = [owner for owner, result in results.items() if result["ok"]]
        self.assertEqual(len(winners), 1, results)
        loser = ({"morning", "evening"} - set(winners)).pop()
        self.assertEqual(results[loser]["holder"]["owner"], winners[0])
        self.assertEqual(read_json(self.root / ".shift-lock.json")["owner"], winners[0])

    def test_held_lock_refuses_others_refreshes_holder_and_expires(self):
        self.assertTrue(shiftlock.run("acquire", "morning", ttl_hours=4)["ok"])
        self.clock = AT + dt.timedelta(hours=3)
        code, held = main("lock", "acquire", "--owner", "evening")
        self.assertEqual(code, 1)
        self.assertEqual((held["holder"]["owner"], held["holder"]["remaining_minutes"]), ("morning", 60))
        refreshed = shiftlock.run("acquire", "morning", ttl_hours=4)
        self.assertTrue(refreshed["refreshed"])
        self.assertEqual(refreshed["holder"]["acquired_at"], "2026-11-04T09:00:00+00:00")
        self.assertEqual(refreshed["holder"]["expires_at"], "2026-11-04T16:00:00+00:00")
        self.clock = AT + dt.timedelta(hours=7, minutes=1)
        self.assertEqual(main("lock", "status")[1], {"ok": True, "free": True, "expired_holder": "morning"})
        code, taken = main("lock", "acquire", "--owner", "evening", "--ttl-hours", "2")
        self.assertEqual(code, 0)
        self.assertEqual((taken["holder"]["owner"], taken["expired_holder"]), ("evening", "morning"))
        self.assertEqual(shiftlock.run("status")["holder"]["remaining_minutes"], 120)

    def test_only_the_holder_releases_without_force(self):
        shiftlock.run("acquire", "morning")
        code, refused = main("lock", "release", "--owner", "evening")
        self.assertEqual(code, 1)
        self.assertEqual(refused["holder"]["owner"], "morning")
        self.assertEqual(shiftlock.run("status")["holder"]["owner"], "morning")
        self.assertEqual(shiftlock.run("release", "evening", force=True), {"ok": True, "released": "morning"})
        self.assertTrue(shiftlock.run("status")["free"])
        shiftlock.run("acquire", "evening")
        self.assertEqual(shiftlock.run("release", "evening"), {"ok": True, "released": "evening"})

    def test_unreadable_lock_file_fails_closed(self):
        (self.root / ".shift-lock.json").write_text("{")
        for action in ("acquire", "status", "release"):
            result = shiftlock.run(action, "evening")
            self.assertFalse(result["ok"])
            self.assertIn("release --force", result["reason"])
        self.assertTrue(shiftlock.run("release", "evening", force=True)["ok"])
        self.assertTrue(shiftlock.run("acquire", "evening")["ok"])


class FactoryCommit(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name).resolve()
        self.gitleaks = Mock(returncode=0, stdout="[]", stderr="")
        self.scans, self.scan_envs = [], []
        self.during_scan = lambda: None
        real_run = commit.run

        def fake_run(command, **kwargs):
            if command[0] == "gitleaks":
                self.scans.append(command)
                self.scan_envs.append(kwargs.get("env"))
                self.during_scan()
                if self.gitleaks is None:
                    return real_run(command, **kwargs)
                if isinstance(self.gitleaks, Exception):
                    raise self.gitleaks
                return self.gitleaks
            return real_run(command, **kwargs)

        self.push = Mock(return_value=0)
        for mocker in (patch.dict(os.environ, HERMETIC), patch.object(commit, "ROOT", self.root),
                       patch.object(commit, "run", side_effect=fake_run), patch.object(publication, "run", self.push)):
            mocker.start()
            self.addCleanup(mocker.stop)
        sh(self.root, "git", "init", "-q", "-b", "main")
        for name in ("notes.md", "ledger.md"):
            (self.root / name).write_text("seed\n")
        sh(self.root, "git", "add", ".")
        sh(self.root, "git", "-c", "user.name=seed", "-c", "user.email=seed@users.noreply.github.com", "commit", "-qm", "seed")
        self.base = self.head()

    def head(self):
        return sh(self.root, "git", "rev-parse", "HEAD")

    def index(self):
        return sh(self.root, "git", "ls-files", "--stage"), sh(self.root, "git", "diff", "--cached", "--name-status")

    def prestage(self):
        """One change staged by hand and one left unstaged, so a refusal must restore both."""
        (self.root / "ledger.md").write_text("staged by hand\n")
        sh(self.root, "git", "add", "ledger.md")
        (self.root / "notes.md").write_text("not staged yet\n")
        return self.index()

    def test_commits_only_explicit_paths_as_the_factory_and_pushes(self):
        (self.root / "notes.md").write_text("shift notes\n")
        (self.root / "ledger.md").unlink()
        new = self.root / "runs" / "report.md"
        new.parent.mkdir()
        new.write_text("report\n")
        code, result = main("commit", "-m", "Morning shift", "--path", str(new))
        self.assertEqual(code, 0, result)
        self.assertEqual(result, {"ok": True, "commit": self.head(), "pushed": True,
                                  "staged": ["A runs/report.md"], "refused_reason": None})
        self.assertEqual(sh(self.root, "git", "show", "HEAD:notes.md"), "seed")
        self.assertEqual(sh(self.root, "git", "show", "HEAD:ledger.md"), "seed")
        self.assertEqual(sh(self.root, "git", "diff", "--name-only").splitlines(), ["ledger.md", "notes.md"])
        self.assertNotEqual(self.head(), self.base)
        self.assertEqual(sh(self.root, "git", "log", "-1", "--format=%an <%ae> %cn <%ce> %s"),
                         f"{CONFIG['git_name']} <{CONFIG['git_email']}> {CONFIG['git_name']} <{CONFIG['git_email']}> Morning shift")
        self.push.assert_called_once_with("push", ["origin", "main"], cwd=self.root)
        self.assertEqual(self.scans[0][:5], ["gitleaks", "protect", "--staged", "--redact", "--no-banner"])
        self.assertIn(str(leaks.CONFIG_FILE), self.scans[0])
        self.assertIn("--ignore-gitleaks-allow", self.scans[0])
        self.assertEqual(self.scans[0][self.scans[0].index("--gitleaks-ignore-path") + 1], os.devnull)

        (self.root / "notes.md").write_text("evening notes\n")
        code, result = main("commit", "-m", "Evening shift", "--no-push")
        self.assertEqual((code, result["pushed"], result["commit"]), (0, False, self.head()))
        self.push.assert_called_once()

    def test_non_main_and_detached_heads_refuse_before_staging_even_without_push(self):
        before = self.prestage()
        sh(self.root, "git", "branch", "shift-records")
        for branch in ("shift-records", self.base):
            sh(self.root, "git", "checkout", branch)
            for push in (True, False):
                with self.subTest(branch=branch, push=push):
                    result = commit.run_commit("records", push=push)
                    self.assertFalse(result["ok"])
                    self.assertIn("factory records must be committed on main", result["refused_reason"])
                    self.assertIn("do not create a temporary branch", result["refused_reason"])
                    self.assertEqual((self.index(), self.head()), (before, self.base))
        self.assertEqual(self.scans, [])
        self.push.assert_not_called()

    def test_explicit_paths_refuse_unrelated_staging_without_changing_it(self):
        before = self.prestage()
        result = commit.run_commit("selected", paths=[str(self.root / "notes.md")], push=False)
        self.assertFalse(result["ok"])
        self.assertIn("staged changes outside --path", result["refused_reason"])
        self.assertEqual(self.index(), before)
        self.assertEqual(self.head(), self.base)
        self.assertEqual(self.scans, [])

    def test_selected_staged_deletion_still_refuses_unrelated_staging(self):
        (self.root / "ledger.md").write_text("unrelated staged record\n")
        sh(self.root, "git", "add", "ledger.md")
        sh(self.root, "git", "rm", "notes.md")
        before = self.index()
        result = commit.run_commit("selected deletion", paths=[str(self.root / "notes.md")], push=False)
        self.assertFalse(result["ok"])
        self.assertIn("staged changes outside --path", result["refused_reason"])
        self.assertEqual((self.index(), self.head()), (before, self.base))
        self.assertEqual(self.scans, [])

    def test_staged_deletion_does_not_skip_a_recreated_file_or_broken_symlink(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                sh(self.root, "git", "rm", "notes.md")
                path = self.root / "notes.md"
                if symlink:
                    path.symlink_to("missing.txt")
                else:
                    path.write_text("replacement\n")
                result = commit.run_commit("replacement", paths=[str(path)], push=False)
                self.assertTrue(result["ok"], result)
                self.assertEqual(sh(self.root, "git", "show", "HEAD:notes.md"), "missing.txt" if symlink else "replacement")

    def test_missing_selected_path_is_not_mistaken_for_a_staged_deletion(self):
        sh(self.root, "git", "rm", "notes.md")
        before = self.index()
        paths = [str(self.root / name) for name in ("notes.md", "misspelled.txt")]
        result = commit.run_commit("missing record", paths=paths, push=False)
        self.assertFalse(result["ok"])
        self.assertIn("misspelled.txt", result["refused_reason"])
        self.assertEqual((self.index(), self.head()), (before, self.base))
        self.assertEqual(self.scans, [])

    def test_ignored_path_refusal_restores_preexisting_staging(self):
        before = self.prestage()
        (self.root / ".gitignore").write_text("*.log\n")
        ignored = self.root / "factory-suite.log"
        ignored.write_text("test output\n")
        index = self.root / ".git/index"
        original = index.read_bytes()
        result = commit.run_commit("records", paths=[str(self.root / "ledger.md"),
                                   str(self.root / "notes.md"), str(ignored)], push=False)
        self.assertFalse(result["ok"])
        self.assertIn("ignored", result["refused_reason"])
        self.assertIn(commit.LOG_POLICY, result["refused_reason"])
        self.assertIn("shift's run directory", result["refused_reason"])
        self.assertIn("rename them to .txt", result["refused_reason"])
        self.assertEqual(index.read_bytes(), original)
        self.assertEqual(self.index(), before)
        self.assertEqual(self.head(), self.base)
        self.assertEqual(self.scans, [])
        self.push.assert_not_called()

    def test_selected_directory_handles_deleted_and_literal_paths(self):
        directory = self.root / "records [owned]"
        directory.mkdir()
        (directory / "old.txt").write_text("old\n")
        sh(self.root, "git", "add", ".")
        sh(self.root, "git", "-c", "user.name=seed", "-c", "user.email=seed@users.noreply.github.com", "commit", "-qm", "record")
        (directory / "old.txt").unlink()
        (directory / "new.txt").write_text("new\n")
        (self.root / "notes.md").write_text("another shift\n")
        sh(self.root, "git", "--literal-pathspecs", "add", "--", str(directory))
        result = commit.run_commit("records", paths=[str(directory / ".." / directory.name)], push=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["staged"], ["A records [owned]/new.txt", "D records [owned]/old.txt"])
        self.assertEqual(sh(self.root, "git", "diff", "--name-only"), "notes.md")

    def test_oversized_file_is_refused_and_index_restored(self):
        before = self.prestage()
        big = self.root / "trace.bin"
        big.write_bytes(b"\0" * (commit.LIMIT + 1))
        code, result = main("commit", "-m", "evidence", "--path", str(big), "--path", str(self.root / "ledger.md"))
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertIn(f"trace.bin ({commit.LIMIT + 1} bytes)", result["refused_reason"])
        self.assertEqual((result["commit"], result["pushed"]), (None, False))
        self.assertEqual(self.index(), before)
        self.assertEqual(self.head(), self.base)
        self.push.assert_not_called()

    def test_gitleaks_failure_refuses_and_restores_index(self):
        for proc, reason in ((Mock(returncode=1, stdout=json.dumps(LEAK), stderr="leaks found: 1"), "gitleaks found 1 leak(s): gitlab-pat in notes.md:2"),
                             (Mock(returncode=126, stdout="", stderr="permission denied"), "gitleaks exited 126")):
            with self.subTest(reason=reason):
                self.gitleaks = proc
                before = self.prestage()
                result = commit.run_commit("notes")
                self.assertFalse(result["ok"])
                self.assertIn(reason, result["refused_reason"])
                self.assertEqual(self.index(), before)
                self.assertEqual(self.head(), self.base)
                self.push.assert_not_called()
        self.gitleaks = OSError("gitleaks not found")
        self.assertIn("gitleaks could not run", commit.run_commit("notes")["refused_reason"])

    def test_gitleaks_errors_behind_a_zero_exit_refuse_the_commit(self):
        """gitleaks exits 0 when git diff fails under it or the scan stops early; only its log says so."""
        for line in ("5:26PM ERR failed to scan Git repository error=\"exit status 128\"",
                     "5:26PM WRN stderr is not empty: fatal: unable to read 3f58f45",
                     "5:26PM WRN no leaks found in partial scan"):
            with self.subTest(line=line):
                self.gitleaks = Mock(returncode=0, stdout="[]", stderr=f"5:26PM INF 0 commits scanned.\n{line}\n")
                before = self.prestage()
                code, result = main("commit", "-m", "notes")
                self.assertEqual((code, result["ok"], result["commit"]), (1, False, None))
                self.assertEqual(result["refused_reason"], f"gitleaks exited 0 but the scan did not finish: {line}")
                self.assertEqual((self.index(), self.head()), (before, self.base))
                self.push.assert_not_called()
        self.gitleaks = Mock(returncode=0, stdout="[]", stderr="5:26PM INF 0 commits scanned.\n"
                             "5:26PM INF scanned ~3628 bytes (3.63 KB) in 27.9ms\n5:26PM INF no leaks found\n")
        self.assertTrue(commit.run_commit("notes")["ok"])
        self.push.assert_called_once()

    def test_refusal_keeps_an_edit_made_in_the_index_second_visible(self):
        """Git re-reads an entry whose file changed in the same second as the index; a restored index with a new mtime would hide the edit."""
        for key, value in (("core.checkStat", "minimal"), ("core.trustCtime", "false")):
            sh(self.root, "git", "config", key, value)
        ledger, index = self.root / "ledger.md", self.root / ".git" / "index"
        second = (time.time_ns() // 10**9 - 3600) * 10**9
        os.utime(ledger, ns=(second, second))
        sh(self.root, "git", "update-index", "--refresh")
        ledger.write_text("SEED\n")
        for path in (ledger, index):
            os.utime(path, ns=(second, second))
        self.assertEqual(sh(self.root, "git", "--no-optional-locks", "diff", "--name-only"), "ledger.md")
        self.gitleaks = Mock(returncode=1, stdout=json.dumps(LEAK), stderr="")
        result = commit.run_commit("ledger", push=False)
        self.assertEqual((result["ok"], result["staged"]), (False, ["M ledger.md"]))
        self.assertEqual(index.stat().st_mtime_ns, second)
        self.assertEqual(sh(self.root, "git", "--no-optional-locks", "diff", "--name-only"), "ledger.md")
        self.gitleaks = Mock(returncode=0, stdout="[]", stderr="")
        self.assertEqual(commit.run_commit("ledger", push=False)["staged"], ["M ledger.md"])
        self.assertEqual(sh(self.root, "git", "show", "HEAD:ledger.md"), "SEED")

    def test_refusal_leaves_an_index_another_git_process_changed(self):
        """Session B commits during A's scan: putting A's old index back would stage the deletion of B's file."""
        def session_b_commits():
            (self.root / "b-evidence.md").write_text("b\n")
            sh(self.root, "git", "add", "b-evidence.md")
            sh(self.root, "git", "-c", "user.name=b", "-c", "user.email=b@users.noreply.github.com", "commit", "-qm", "b", "--", "b-evidence.md")

        (self.root / "notes.md").write_text("a notes\n")
        self.during_scan = session_b_commits
        self.gitleaks = Mock(returncode=1, stdout=json.dumps(LEAK), stderr="")
        result = commit.run_commit("a", push=False)
        self.assertFalse(result["ok"])
        self.assertIn("gitleaks found 1 leak(s)", result["refused_reason"])
        self.assertIn("still holds what this command staged: another git process changed HEAD or the index", result["refused_reason"])
        self.assertEqual(self.index()[1], "M\tnotes.md")
        self.during_scan = lambda: None
        self.gitleaks = Mock(returncode=0, stdout="[]", stderr="")
        self.assertEqual(commit.run_commit("a", push=False)["staged"], ["M notes.md"])
        self.assertEqual(sh(self.root, "git", "ls-tree", "--name-only", "HEAD").split(), ["b-evidence.md", "ledger.md", "notes.md"])

    def test_refusal_never_writes_past_another_process_index_lock(self):
        lock, index = self.root / ".git" / "index.lock", self.root / ".git" / "index"
        (self.root / "notes.md").write_text("a notes\n")
        self.during_scan = lambda: lock.write_bytes(b"theirs")
        self.gitleaks = Mock(returncode=1, stdout=json.dumps(LEAK), stderr="")
        result = commit.run_commit("a", push=False)
        self.assertIn(f"could not take {lock} (File exists)", result["refused_reason"])
        self.assertEqual((self.index()[1], lock.read_bytes()), ("M\tnotes.md", b"theirs"))
        self.during_scan = lambda: None
        written = index.stat().st_mtime_ns
        result = commit.run_commit("b", push=False)
        self.assertIn("index.lock", result["refused_reason"])
        self.assertEqual((index.stat().st_mtime_ns, lock.read_bytes()), (written, b"theirs"))
        self.assertEqual(len(self.scans), 1)

    def test_commit_rejected_by_a_hook_restores_the_index(self):
        hooks = self.root / ".git" / "rejecting-hooks"
        hooks.mkdir()
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
        (hooks / "pre-commit").chmod(0o755)
        sh(self.root, "git", "config", "core.hooksPath", str(hooks))
        before = self.prestage()
        result = commit.run_commit("hooked", push=False)
        self.assertFalse(result["ok"])
        self.assertIn("commit -q -m hooked failed (1)", result["refused_reason"])
        self.assertNotIn("still holds", result["refused_reason"])
        self.assertEqual((self.index(), self.head()), (before, self.base))

    def test_file_staged_during_the_scan_is_refused_before_committing(self):
        """A hook that stages a file while gitleaks runs would put an unscanned file in the commit."""
        trees = {}

        def hook_stages():
            trees["scanned"] = sh(self.root, "git", "write-tree")
            (self.root / "unscanned.md").write_text("unscanned\n")
            sh(self.root, "git", "add", "unscanned.md")
            trees["now"] = sh(self.root, "git", "write-tree")

        (self.root / "notes.md").write_text("a notes\n")
        self.during_scan = hook_stages
        code, result = main("commit", "-m", "a")
        self.assertEqual(code, 1)
        self.assertEqual((result["ok"], result["commit"], result["staged"]), (False, None, ["M notes.md"]))
        self.assertIn(f"(scanned tree {trees['scanned']}, index now {trees['now']}); nothing was committed", result["refused_reason"])
        self.assertIn("still holds what this command staged: another git process changed HEAD or the index", result["refused_reason"])
        self.assertEqual(self.index()[1], "M\tnotes.md\nA\tunscanned.md")
        self.assertEqual(self.head(), self.base)
        self.push.assert_not_called()

    def test_tree_changed_by_the_commit_step_stays_local_and_is_not_pushed(self):
        """A pre-commit hook that stages a file changes the tree after the last check; only the committed tree shows it."""
        real, trees = commit.commit, {}

        def hooked_commit(message):
            trees["scanned"] = sh(self.root, "git", "write-tree")
            (self.root / "unscanned.md").write_text("unscanned\n")
            sh(self.root, "git", "add", "unscanned.md")
            real(message)

        (self.root / "notes.md").write_text("a notes\n")
        with patch.object(commit, "commit", side_effect=hooked_commit):
            code, result = main("commit", "-m", "a")
        head, committed = self.head(), sh(self.root, "git", "rev-parse", "HEAD^{tree}")
        self.assertEqual(code, 1)
        self.assertEqual(result, {"ok": False, "commit": head, "pushed": False, "staged": ["M notes.md"],
                                  "refused_reason": f"commit {head} has tree {committed}, not the scanned tree {trees['scanned']}; "
                                                    "it stays local and was not pushed"})
        self.assertEqual(sh(self.root, "git", "rev-parse", "HEAD~1"), self.base)
        self.push.assert_not_called()

    def test_scan_uses_the_scrubbed_environment_of_leaks_scan(self):
        (self.root / "notes.md").write_text("shift notes\n")
        with patch.dict(os.environ, {"GITLEAKS_CONFIG": "/elsewhere.toml", "GIT_TERMINAL_PROMPT": "0"}):
            self.assertTrue(commit.run_commit("notes", push=False)["ok"])
            with patch.object(leaks.shutil, "which", return_value="gitleaks"), \
                    patch.object(leaks.subprocess, "run", return_value=Mock(returncode=0, stdout="[]")) as scanned:
                self.assertEqual(leaks.scan(text="clean"), [])
        self.assertEqual(self.scan_envs, [scanned.call_args.kwargs["env"]])
        self.assertNotIn("GITLEAKS_CONFIG", self.scan_envs[0])
        self.assertNotIn("GIT_TERMINAL_PROMPT", self.scan_envs[0])

    def test_in_band_gitleaks_waivers_do_not_let_a_secret_through(self):
        self.gitleaks = None
        token = "glpat-" + "Ab3dEf7hIj9kLm2nOp4q"
        (self.root / "notes.md").write_text(f'token = "{token}" # gitleaks:allow\n')
        before = self.index()
        result = commit.run_commit("allow comment", push=False)
        self.assertFalse(result["ok"], result)
        self.assertIn("gitleaks found 1 leak(s): gitlab-pat in notes.md:1", result["refused_reason"])
        self.assertNotIn(token, json.dumps(result))
        self.assertEqual(self.index(), before)
        (self.root / ".gitleaksignore").write_text("notes.md:gitlab-pat:1\n")
        result = commit.run_commit("ignore file", push=False)
        self.assertFalse(result["ok"], result)
        self.assertIn(".gitleaksignore", result["refused_reason"])
        self.assertEqual((self.index(), self.head()), (before, self.base))
        self.assertEqual(len(self.scans), 1)
        self.push.assert_not_called()

    def test_pinned_git_environment_is_refused_before_staging(self):
        before = self.prestage()
        other = self.root / "other-index"
        for key, value in (("GIT_INDEX_FILE", str(other)), ("GIT_DIR", str(self.root / ".git"))):
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                result = commit.run_commit("pinned", push=False)
                self.assertFalse(result["ok"])
                self.assertIn(f"unset {key}", result["refused_reason"])
        self.assertFalse(other.exists())
        self.assertEqual((self.index(), self.head()), (before, self.base))
        self.assertEqual(self.scans, [])

    def test_nothing_staged_is_ok_without_a_commit(self):
        code, result = main("commit", "-m", "nothing")
        self.assertEqual(code, 0)
        self.assertEqual(result, {"ok": True, "commit": None, "pushed": False, "staged": [], "refused_reason": None})
        self.assertEqual(self.head(), self.base)
        self.assertEqual(self.scans, [])
        self.push.assert_not_called()

    def test_path_outside_the_factory_is_refused_before_staging(self):
        before = self.prestage()
        with tempfile.NamedTemporaryFile(suffix=".md") as outside:
            result = commit.run_commit("outside", [outside.name])
        self.assertFalse(result["ok"])
        self.assertIn("paths outside the factory", result["refused_reason"])
        self.assertEqual(self.index(), before)
        self.assertEqual(self.head(), self.base)


if __name__ == "__main__":
    unittest.main()
