"""The headless shift runner schedules by local time and summarises a transcript without quoting it whole."""
import datetime as dt
import fcntl
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import setup_module
from factory.core import shiftlock

SPEC = importlib.util.spec_from_file_location("agent_shift", ROOT / "schedule" / "agent-shift.py")
agent_shift = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent_shift)


def setUpModule():
    setup_module(unittest)


class ShiftLease(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.logs = self.root / "shift-logs"
        self.logs.mkdir()
        self.name = "auto-1112-2245"
        self.router = Mock()
        self.router.stdout.readline.return_value = "12345\n"
        self.session = Mock()
        self.popen = Mock(side_effect=lambda *args, **kwargs: self.session if "cwd" in kwargs else self.router)
        self.watch = Mock(return_value=0)
        self.sleep = Mock()
        for mocker in (patch.object(shiftlock, "ROOT", self.root), patch.object(agent_shift, "ROOT", self.root),
                       patch.object(agent_shift, "logs_dir", return_value=self.logs),
                       patch.object(agent_shift, "load_config", return_value={"public": {}}),
                       patch.object(agent_shift.subprocess, "Popen", self.popen),
                       patch.object(agent_shift.subprocess, "run"), patch.object(agent_shift.time, "sleep", self.sleep),
                       patch.object(agent_shift, "wait_for_github", return_value={"ok": True}),
                       patch.object(agent_shift, "watch", self.watch)):
            mocker.start()
            self.addCleanup(mocker.stop)

    def acquire(self, owner):
        result = shiftlock.run("acquire", owner)
        self.assertTrue(result["ok"], result)
        return result["holder"]

    def saved_summary(self):
        return json.loads((self.logs / self.name / "summary.json").read_text())

    def runner_record(self, owner):
        directory = self.logs / owner
        directory.mkdir(exist_ok=True)
        marker = directory / "runner.lock"
        marker.write_text(str(os.getpid()) + "\n")
        return marker

    def test_start_and_resume_tasks_require_records_directly_on_main(self):
        for template in (agent_shift.PROMPT, agent_shift.RESUME):
            with self.subTest(template=template):
                prompt = template.format(name=self.name)
                self.assertIn(self.name, prompt)
                self.assertIn("本人要求工厂记录直接在 main 上用 `bin/factory commit` 提交，不建临时分支。", prompt)

    def test_fresh_and_error_resume_commands_append_the_main_record_rule(self):
        def finish(session, transcript, deadline):
            transcript.write_text(json.dumps({"type": "assistant", "session_id": "session-1"}) + "\n")
            return 1 if self.watch.call_count == 1 else 0

        self.watch.side_effect = finish
        summary = agent_shift.run_shift(self.name, 240)
        self.assertEqual(summary["attempts"], [1, 0])
        sessions = [call for call in self.popen.call_args_list if "cwd" in call.kwargs]
        self.assertEqual(len(sessions), 2)
        self.assertNotIn("--resume", sessions[0].args[0])
        self.assertEqual(sessions[1].args[0][-2:], ["--resume", "session-1"])
        for call in sessions:
            command = call.args[0]
            self.assertEqual(command.count("--append-system-prompt"), 1)
            prompt = command[command.index("--append-system-prompt") + 1]
            self.assertEqual(prompt, agent_shift.SYSTEM_PROMPT)
            for text in ("repository owner explicitly requires", "directly on main", "bin/factory commit",
                         "user's instruction", "overrides the default", "Never create a temporary branch", "owner action"):
                self.assertIn(text, prompt)

    def test_github_unavailable_never_starts_or_touches_the_lease(self):
        held = self.acquire("auto-previous")
        self.runner_record("auto-previous")
        access = {"ok": False, "attempts": 40, "wait_seconds": 1200,
                  "reason": "macOS login keychain is locked (-25293)"}
        with patch.object(agent_shift, "wait_for_github", return_value=access), \
                patch.object(agent_shift, "prepare_shift") as prepare:
            summary = agent_shift.run_shift(self.name, 240)
        self.assertIn("GitHub unavailable before session start", summary["status"])
        self.assertEqual(summary["github_access"], access)
        self.assertEqual(summary["attempts"], [])
        self.assertEqual(summary, self.saved_summary())
        prepare.assert_not_called()
        self.popen.assert_not_called()
        self.assertEqual(shiftlock.run("status")["holder"], held)

    def test_own_lease_is_released_after_failed_run(self):
        for outcome in (1, "no answer after start", "stopped at the time limit"):
            with self.subTest(outcome=outcome):
                def fail(*args):
                    self.acquire(self.name)
                    return outcome

                self.watch.side_effect = fail
                summary = agent_shift.run_shift(self.name, 240)
                expected = 1 if outcome == "stopped at the time limit" else agent_shift.ATTEMPTS
                self.assertEqual(summary["attempts"], [outcome] * expected)
                self.assertEqual(summary["status"], "exited 1" if outcome == 1 else outcome)
                self.assertEqual(summary, self.saved_summary())
                self.assertTrue(shiftlock.run("status")["free"])
                self.assertFalse((self.root / shiftlock.STATE).exists())

    def test_startup_errors_release_own_lease_and_write_summary(self):
        for target in ("router", "session", "port"):
            with self.subTest(target=target):
                def start(*args, **kwargs):
                    if (target == "router" or "cwd" in kwargs) and target != "port":
                        self.acquire(self.name)
                        raise OSError("start failed")
                    return self.router

                def port():
                    if target == "port":
                        self.acquire(self.name)
                        return "not a port"
                    return "12345\n"

                self.popen.side_effect = start
                self.router.stdout.readline.side_effect = port
                summary = agent_shift.run_shift(self.name, 240)
                self.assertTrue(summary["status"].startswith("could not start:"))
                self.assertEqual(summary["attempts"], [])
                self.assertEqual(summary, self.saved_summary())
                self.assertFalse((self.root / shiftlock.STATE).exists())
        self.watch.assert_not_called()

    def test_failure_cleanup_never_releases_a_different_owner(self):
        def fail(*args):
            self.acquire("interactive")
            return "stopped at the time limit"

        self.watch.side_effect = fail
        agent_shift.run_shift(self.name, 240)
        self.assertEqual(shiftlock.run("status")["holder"]["owner"], "interactive")

    def test_clean_exit_after_releasing_lease_is_done_without_resume_or_cleanup(self):
        def succeed(*args):
            self.acquire(self.name)
            self.assertTrue(shiftlock.run("release", self.name)["ok"])
            return 0

        self.watch.side_effect = succeed
        with patch.object(shiftlock, "run", wraps=shiftlock.run) as lease:
            summary = agent_shift.run_shift(self.name, 240)
        self.assertEqual(summary["status"], "exited 0")
        self.assertEqual(summary["attempts"], [0])
        self.assertEqual(summary, self.saved_summary())
        self.assertTrue(shiftlock.run("status")["free"])
        self.assertEqual([call.args for call in lease.call_args_list if call.args[0] == "release"],
                         [("release", self.name)])
        self.watch.assert_called_once()
        self.sleep.assert_not_called()

    def test_clean_exit_with_own_lease_resumes_the_same_session_until_closed(self):
        def finish(session, transcript, deadline):
            if self.watch.call_count == 1:
                self.acquire(self.name)
                transcript.write_text(json.dumps({"type": "assistant", "session_id": "session-1"}) + "\n")
            else:
                self.assertEqual(shiftlock.run("status")["holder"]["owner"], self.name)
                self.assertTrue(shiftlock.run("release", self.name)["ok"])
            return 0

        self.watch.side_effect = finish
        summary = agent_shift.run_shift(self.name, 240)
        self.assertEqual(summary["status"], "exited 0")
        self.assertEqual(summary["attempts"], ["exited 0 with the shift still open", 0])
        self.assertEqual(summary, self.saved_summary())
        sessions = [call for call in self.popen.call_args_list if "cwd" in call.kwargs]
        self.assertEqual(len(sessions), 2)
        self.assertNotIn("--resume", sessions[0].args[0])
        self.assertEqual(sessions[1].args[0][-2:], ["--resume", "session-1"])
        prompt = sessions[1].args[0][2]
        for text in (self.name, "not closed yet", "background work", "bin/factory next",
                     f"bin/factory shift end --owner {self.name}", "schedule/session-shift.md",
                     "bin/factory commit", "main"):
            self.assertIn(text, prompt)
        for call in sessions:
            command = call.args[0]
            self.assertEqual(command[command.index("--append-system-prompt") + 1], agent_shift.SYSTEM_PROMPT)
            self.assertEqual(call.kwargs["env"]["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"], "0")
        self.assertEqual(self.watch.call_args_list[0].args[2], self.watch.call_args_list[1].args[2])
        self.sleep.assert_called_once_with(agent_shift.RESUME_PAUSE)
        self.assertTrue(shiftlock.run("status")["free"])

    def test_last_attempt_still_open_records_failure_and_releases_own_lease(self):
        def remain_open(session, transcript, deadline):
            self.acquire(self.name)
            transcript.write_text(json.dumps({"type": "assistant", "session_id": "session-1"}) + "\n")
            return 0

        self.watch.side_effect = remain_open
        summary = agent_shift.run_shift(self.name, 240)
        self.assertEqual(summary["status"], "ended with the shift still open")
        self.assertEqual(summary["attempts"], ["exited 0 with the shift still open"] * agent_shift.ATTEMPTS)
        self.assertEqual(summary, self.saved_summary())
        self.assertEqual(self.watch.call_count, agent_shift.ATTEMPTS)
        self.assertEqual(self.sleep.call_count, agent_shift.ATTEMPTS - 1)
        self.assertTrue(shiftlock.run("status")["free"])
        self.assertFalse((self.root / shiftlock.STATE).exists())

    def test_open_shift_without_time_for_resume_is_failed_and_released(self):
        clock = [1000]
        def remain_open(session, transcript, deadline):
            self.acquire(self.name)
            clock[0] = deadline - 1
            return 0

        self.watch.side_effect = remain_open
        with patch.object(agent_shift.time, "time", side_effect=lambda: clock[0]):
            summary = agent_shift.run_shift(self.name, 1)
        self.assertEqual(summary["status"], "ended with the shift still open")
        self.assertEqual(summary["attempts"], ["exited 0 with the shift still open"])
        self.assertEqual(summary, self.saved_summary())
        self.watch.assert_called_once()
        self.sleep.assert_not_called()
        self.assertTrue(shiftlock.run("status")["free"])

    def test_clean_exit_never_resumes_or_releases_a_different_owner(self):
        def succeed(*args):
            self.acquire("interactive")
            return 0

        self.watch.side_effect = succeed
        summary = agent_shift.run_shift(self.name, 240)
        self.assertEqual(summary["status"], "exited 0")
        self.assertEqual(summary["attempts"], [0])
        self.assertEqual(shiftlock.run("status")["holder"]["owner"], "interactive")
        self.watch.assert_called_once()
        self.sleep.assert_not_called()

    def test_unreadable_lease_after_clean_exit_is_not_a_success_or_retried(self):
        def corrupt(*args):
            (self.root / shiftlock.STATE).write_text("{")
            return 0

        self.watch.side_effect = corrupt
        summary = agent_shift.run_shift(self.name, 240)
        self.assertIn("lease check failed after exit 0", summary["status"])
        self.assertEqual(len(summary["attempts"]), 1)
        self.assertIn("lease check failed after exit 0", summary["attempts"][0])
        self.assertEqual(summary, self.saved_summary())
        self.watch.assert_called_once()
        self.sleep.assert_not_called()
        self.assertEqual((self.root / shiftlock.STATE).read_text(), "{")

    def test_dead_auto_runner_lease_is_released_before_start(self):
        owner = "auto-1112-2143"
        self.acquire(owner)
        marker = self.runner_record(owner)
        with marker.open() as runner:
            fcntl.flock(runner, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def start(*args, **kwargs):
            self.assertTrue(shiftlock.run("status")["free"])
            return self.session if "cwd" in kwargs else self.router

        self.popen.side_effect = start
        summary = agent_shift.run_shift(self.name, 240)
        self.assertEqual(summary["status"], "exited 0")
        self.assertEqual(summary["stale_lease_released"], owner)
        self.assertEqual(summary, self.saved_summary())
        self.watch.assert_called_once()

    def test_finished_legacy_auto_shift_can_be_recovered(self):
        owner = "auto-1112-2143"
        self.acquire(owner)
        previous = self.logs / owner
        previous.mkdir()
        (previous / "summary.json").write_text('{"status": "exited 1"}\n')
        summary = agent_shift.run_shift(self.name, 240)
        self.assertEqual(summary["stale_lease_released"], owner)
        self.assertTrue(shiftlock.run("status")["free"])

    def test_live_auto_runner_is_left_alone_even_with_an_old_summary(self):
        owner = "auto-1112-2143"
        held = self.acquire(owner)
        marker = self.runner_record(owner)
        (marker.parent / "summary.json").write_text('{"status": "exited 0"}\n')
        with marker.open() as runner:
            fcntl.flock(runner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            summary = agent_shift.run_shift(self.name, 240)
        self.assertEqual(summary["status"], f"blocked: shift held by {owner} (runner alive)")
        self.assertEqual(summary["attempts"], [])
        self.assertEqual(summary, self.saved_summary())
        self.assertNotIn("stale_lease_released", summary)
        self.assertEqual(shiftlock.run("status")["holder"], held)
        self.popen.assert_not_called()

    def test_non_agent_lease_is_left_alone(self):
        owner = "interactive"
        held = self.acquire(owner)
        self.runner_record(owner)
        summary = agent_shift.run_shift(self.name, 240)
        self.assertEqual(summary["status"], f"blocked: shift held by {owner}")
        self.assertEqual(summary["attempts"], [])
        self.assertEqual(summary, self.saved_summary())
        self.assertEqual(shiftlock.run("status")["holder"], held)
        self.popen.assert_not_called()

    def test_auto_lease_without_runner_evidence_is_left_alone(self):
        owner = "auto-1112-2143"
        held = self.acquire(owner)
        summary = agent_shift.run_shift(self.name, 240)
        self.assertIn("no runner record", summary["status"])
        self.assertEqual(shiftlock.run("status")["holder"], held)
        self.popen.assert_not_called()

    def test_stale_recovery_does_not_release_a_superseding_generation(self):
        owner = "auto-1112-2143"
        original = self.acquire(owner)
        self.runner_record(owner)
        run = shiftlock.run

        def supersede(action, *args, **kwargs):
            if action == "release":
                self.acquire(owner)
            return run(action, *args, **kwargs)

        with patch.object(shiftlock, "run", side_effect=supersede):
            summary = agent_shift.run_shift(self.name, 240)
        self.assertIn("superseded", summary["status"])
        self.assertNotIn("stale_lease_released", summary)
        self.assertNotEqual(shiftlock.run("status")["holder"]["generation"], original["generation"])
        self.popen.assert_not_called()

    def test_runner_holds_its_lock_until_the_summary_is_written(self):
        def check_lock(*args):
            marker = self.logs / self.name / "runner.lock"
            self.assertEqual(marker.read_text().strip(), str(os.getpid()))
            with marker.open() as runner, self.assertRaises(BlockingIOError):
                fcntl.flock(runner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return 0

        self.watch.side_effect = check_lock
        summary = agent_shift.run_shift(self.name, 240)
        with (self.logs / self.name / "runner.lock").open() as runner:
            fcntl.flock(runner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(summary, self.saved_summary())

    def test_public_refresh_is_recorded_after_summary_without_retrying_shift(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                refreshed = {"ok": True, "changed": True, "commit": "a" * 40, "pushed": True}
                def run(command, **kwargs):
                    if "publish" in command:
                        self.assertEqual(self.saved_summary()["status"], "exited 0")
                        self.assertNotIn("public_refresh", self.saved_summary())
                        if fails:
                            raise OSError("Synthetic refresh failure")
                        return Mock(returncode=0, stdout=json.dumps(refreshed))
                    return Mock(returncode=0)

                self.watch.reset_mock()
                with patch.object(agent_shift, "load_config", return_value={"public": {"clone": "public-clone"}}), \
                        patch.object(agent_shift.subprocess, "run", side_effect=run) as commands:
                    summary = agent_shift.run_shift(self.name, 240)
                self.assertEqual(summary["status"], "exited 0")
                self.assertEqual(summary["attempts"], [0])
                self.assertEqual(summary, self.saved_summary())
                if fails:
                    self.assertEqual(summary["public_refresh"], {"ok": False, "error": "Synthetic refresh failure"})
                else:
                    self.assertEqual(summary["public_refresh"], refreshed)
                self.assertEqual(sum("publish" in call.args[0] for call in commands.call_args_list), 1)
                self.watch.assert_called_once()
                self.sleep.assert_not_called()

    def test_duplicate_runner_does_not_overwrite_live_records(self):
        marker = self.runner_record(self.name)
        saved = marker.parent / "summary.json"
        saved.write_text("previous summary\n")
        with marker.open() as runner:
            fcntl.flock(runner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(SystemExit, "runner already active"):
                agent_shift.run_shift(self.name, 240)
        self.assertEqual(saved.read_text(), "previous summary\n")
        self.popen.assert_not_called()


class PublicRefresh(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.out = self.root / "shift-logs" / "current"
        self.out.mkdir(parents=True)
        self.config = {"public": {"clone": "public-clone"}}
        self.result = {"ok": True, "changed": False, "commit": "a" * 40, "pushed": False}
        self.run = Mock(return_value=Mock(returncode=0, stdout=json.dumps(self.result)))
        for mocker in (patch.object(agent_shift, "ROOT", self.root),
                       patch.object(agent_shift, "load_config", return_value=self.config),
                       patch.object(agent_shift.subprocess, "run", self.run)):
            mocker.start()
            self.addCleanup(mocker.stop)

    def test_unconfigured_clone_skips_without_subprocess(self):
        self.config["public"].clear()
        self.assertIn("skipped", agent_shift.refresh_public(self.out))
        self.run.assert_not_called()

    def test_command_without_report_is_bounded_and_never_runs_suite(self):
        self.assertEqual(agent_shift.refresh_public(self.out), self.result)
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[0], [str(self.root / "bin/factory"), "public", "publish",
                                                    "--clone", str(self.root / "public-clone")])
        self.assertEqual(self.run.call_args.kwargs["timeout"], 600)
        self.assertEqual(self.run.call_args.kwargs["cwd"], self.root)
        self.assertEqual(self.run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_latest_existing_suite_report_is_reused(self):
        previous = self.out.parent / "previous"
        previous.mkdir()
        reports = [self.out.parent / "suite.json", previous / "suite.json", self.out / "suite.json"]
        for index, path in enumerate(reports):
            path.write_text('{"success": true}')
            os.utime(path, (index + 1, index + 1))
        for expected in reversed(reports):
            agent_shift.refresh_public(self.out)
            self.assertEqual(self.run.call_args.args[0][-2:], ["--suite-report", str(expected)])
            expected.unlink()

    def test_failure_timeout_malformed_output_and_config_errors_never_raise(self):
        for response in (Mock(returncode=2, stderr="Rejected", stdout=""),
                         Mock(returncode=0, stdout="not json"), Mock(returncode=0, stdout="[]"),
                         Mock(returncode=0, stdout='{"ok": false}')):
            self.run.return_value = response
            self.assertFalse(agent_shift.refresh_public(self.out)["ok"])
        for error in (OSError("Not executable"), RuntimeError("Unexpected failure"),
                      agent_shift.subprocess.TimeoutExpired("publish", 600)):
            self.run.side_effect = error
            result = agent_shift.refresh_public(self.out)
            self.assertFalse(result["ok"])
            self.assertTrue(result["error"])
        with patch.object(agent_shift, "load_config", side_effect=ValueError("Invalid config")):
            self.assertEqual(agent_shift.refresh_public(self.out), {"ok": False, "error": "Invalid config"})

    def test_failure_output_is_redacted_before_recording(self):
        with patch.dict(agent_shift.privacy.CONFIG, {"private_terms": ["synthetic-private-marker"]}):
            self.run.return_value = Mock(returncode=2, stdout="", stderr="Failed: synthetic-private-marker")
            result = agent_shift.refresh_public(self.out)
        self.assertEqual(result, {"ok": False, "error": "Failed: [redacted]"})


class GitHubPreflight(unittest.TestCase):
    def test_wait_recovers_after_unlock_and_is_bounded_when_unavailable(self):
        for responses, ok, count, elapsed in ((["locked", None], True, 2, 30), (["locked"] * 3, False, 3, 65)):
            with self.subTest(ok=ok):
                clock = [0]
                def sleep(seconds):
                    clock[0] += seconds
                with patch.object(agent_shift.time, "monotonic", side_effect=lambda: clock[0]), \
                        patch.object(agent_shift.time, "sleep", side_effect=sleep), \
                        patch.object(agent_shift, "github_access", side_effect=responses) as check:
                    result = agent_shift.wait_for_github(65)
                self.assertEqual((result["ok"], result["attempts"], result["wait_seconds"]), (ok, count, elapsed))
                self.assertEqual(check.call_count, count)

    def test_checks_api_then_git_without_fetch_or_interactive_prompts(self):
        with patch.object(agent_shift.subprocess, "run", return_value=Mock(returncode=0)) as run:
            self.assertIsNone(agent_shift.github_access(30))
        self.assertEqual([c.args[0][:2] for c in run.call_args_list], [["gh", "api"], ["git", "ls-remote"]])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(call.kwargs["env"]["PATH"], agent_shift.SESSION_PATH)
            self.assertNotIn("GH_TOKEN", call.kwargs["env"])
            self.assertLessEqual(call.kwargs["timeout"], 30)

    def test_locked_keychain_and_timeout_are_not_bad_login_diagnoses(self):
        with patch.object(agent_shift.subprocess, "run", return_value=Mock(returncode=1, stderr="error -25293")):
            self.assertIn("keychain is locked", agent_shift.github_access(30))
        with patch.object(agent_shift.subprocess, "run", side_effect=agent_shift.subprocess.TimeoutExpired("gh", 30)):
            self.assertIn("timed out", agent_shift.github_access(30))


class Schedule(unittest.TestCase):
    def test_next_start_is_the_first_configured_time_after_now(self):
        day = dt.datetime(2026, 11, 12, 7, 0)
        self.assertEqual(agent_shift.next_start(day, "08:43,21:43"), dt.datetime(2026, 11, 12, 8, 43))
        self.assertEqual(agent_shift.next_start(day.replace(hour=8, minute=43), "08:43,21:43"), dt.datetime(2026, 11, 12, 21, 43))
        self.assertEqual(agent_shift.next_start(day.replace(hour=22), "08:43, 21:43"), dt.datetime(2026, 11, 13, 8, 43))


class Summary(unittest.TestCase):
    def test_summary_counts_main_calls_and_keeps_errors_refusals_and_the_report(self):
        events = [
            {"type": "assistant", "session_id": "s1", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "id": "b1"}, {"type": "tool_use", "name": "Read", "id": "r1"},
                {"type": "text", "text": "x"}]}},
            {"type": "assistant", "parent_tool_use_id": "sub", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "Exit code 1\nboom"}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "b1", "content": "ack refused: closes through observed state"}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "r1", "content": "1 # a document that says a push is blocked"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "fine"}]}},
            {"type": "result", "num_turns": 3, "duration_ms": 120000, "result": "（等待中。）"},
            {"type": "result", "num_turns": 9, "duration_ms": 600000, "result": "这一班做了什么……\n今日合入 1 个、今日提交 2 个"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "transcript.jsonl"
            path.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\nnot json\n")
            summary = agent_shift.summarize(path)
            self.assertEqual(agent_shift.session_id(path), "s1")
        self.assertEqual((summary["tool_calls"], summary["turns"], summary["active_minutes"]), (2, 12, 12.0))
        self.assertEqual(summary["errors"], ["Exit code 1 boom"])
        self.assertEqual(summary["refusals"], ["ack refused: closes through observed state"])
        self.assertIn("今日合入 1 个", summary["final"])

    def test_a_session_that_never_answered_is_started_again_instead_of_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "transcript.jsonl"
            path.write_text(json.dumps({"type": "system", "subtype": "init", "session_id": "s0"}) + "\n")
            self.assertIsNone(agent_shift.session_id(path))
            self.assertFalse(agent_shift.spoke(path, 0))
            with path.open("a") as stream:
                stream.write(json.dumps({"type": "assistant", "session_id": "s0", "message": {"content": []}}) + "\n")
            self.assertEqual(agent_shift.session_id(path), "s0")
            self.assertTrue(agent_shift.spoke(path, 0))

    def test_missing_transcript_gives_an_empty_summary(self):
        summary = agent_shift.summarize(pathlib.Path("/nonexistent/transcript.jsonl"))
        self.assertEqual((summary["tool_calls"], summary["errors"], summary["final"]), (0, [], ""))


if __name__ == "__main__":
    unittest.main()
