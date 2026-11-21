"""The headless shift runner schedules by local time and summarises a transcript without quoting it whole."""
import contextlib
import copy
import datetime as dt
import io
import fcntl
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
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


def file_inventory(root):
    return {str(path.relative_to(root)): ("link", os.readlink(path)) if path.is_symlink() else
            ("directory",) if path.is_dir() else ("file", path.read_bytes()) for path in root.rglob("*")}


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
                       patch.object(agent_shift.select, "select", return_value=([self.router.stdout], [], [])),
                       patch.object(agent_shift.subprocess, "run"), patch.object(agent_shift.time, "sleep", self.sleep),
                       patch.object(agent_shift, "require_runtime", side_effect=agent_shift.runtime_config),
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

    def test_weekly_retry_keeps_review_boundary_and_does_not_publish(self):
        def finish(session, transcript, deadline):
            transcript.write_text(json.dumps({"type": "assistant", "session_id": "weekly-1"}) + "\n")
            return 1 if self.watch.call_count == 1 else 0
        self.watch.side_effect = finish
        with patch.object(agent_shift, "refresh_public") as publish:
            summary = agent_shift.run_shift(self.name, 180, kind="weekly", key="-".join(("weekly", "2026", "W47")))
        self.assertEqual(summary["attempts"], [1, 0])
        publish.assert_not_called()
        sessions = [call for call in self.popen.call_args_list if "cwd" in call.kwargs]
        for call in sessions:
            command = call.args[0]
            self.assertIn("weekly-review.md", command[2])
            self.assertIn("weekly/weekly-2026-W47", command[command.index("--append-system-prompt") + 1])
            self.assertEqual(call.kwargs["env"]["ANTHROPIC_MODEL"], "opus")

    def test_disable_during_router_start_prevents_the_first_session(self):
        for kind in ("daily", "weekly"):
            for scope in ("global", "kind"):
                with self.subTest(kind=kind, scope=scope):
                    config = agent_shift.load_schedule(environ={})
                    def start(spec):
                        if scope == "global":
                            config["enabled"] = False
                        else:
                            config["kinds"][kind]["enabled"] = False
                        return self.router, 12345
                    self.popen.reset_mock()
                    with patch.object(agent_shift, "load_schedule", return_value=config), \
                            patch.object(agent_shift, "start_router", side_effect=start), \
                            patch.object(agent_shift, "refresh_public", return_value={"ok": True}) as publish:
                        summary = agent_shift.run_shift(self.name, 180, kind=kind)
                    self.assertEqual(summary["status"], "disabled")
                    self.assertEqual(summary["attempts"], [])
                    self.popen.assert_not_called()
                    publish.assert_not_called()

    def test_lease_acquired_during_router_start_defers_without_releasing_it(self):
        def start(spec):
            self.acquire("interactive-daily")
            return self.router, 12345
        with patch.object(agent_shift, "start_router", side_effect=start), \
                patch.object(shiftlock, "run", wraps=shiftlock.run) as lease:
            summary = agent_shift.run_shift(self.name, 180, kind="weekly")
        self.assertEqual(summary["status"], "deferred")
        self.assertEqual(summary["attempts"], [])
        self.popen.assert_not_called()
        self.assertEqual(shiftlock.run("status")["holder"]["owner"], "interactive-daily")
        self.assertFalse(any(call.args[0] == "release" for call in lease.call_args_list))

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


class Runtime(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.config = agent_shift.load_schedule(environ={})
        self.config["runtime"] = {
            "path": str(self.root / "tools") + ":/usr/bin:/bin", "python": "/usr/bin/python3",
            "claude": str(self.root / "tools/claude"), "router": str(self.root / "router.py"),
            "router_port": "9001", "shell": "/bin/bash", "notify": str(self.root / "notify"),
            "logs": str(self.root / "production/logs"),
        }
        self.schedule = self.root / "schedule.json"
        self.schedule.write_text(json.dumps(self.config))
        (self.root / "router.py").write_text("pass\n")
        for mocker in (patch.object(agent_shift, "SCHEDULE", self.schedule),
                       patch.object(agent_shift, "ROOT", self.root), patch.object(shiftlock, "ROOT", self.root),
                       patch.dict(os.environ, {}, clear=True)):
            mocker.start()
            self.addCleanup(mocker.stop)

    def test_runtime_environment_overrides_each_configured_preset(self):
        overrides = {variable: ("9002" if field == "router_port" else "/portable/" + field)
                     for field, variable in agent_shift.RUNTIME_ENV.items()}
        self.assertEqual(agent_shift.runtime_config(self.config, environ={}), self.config["runtime"])
        runtime = agent_shift.runtime_config(self.config, overrides)
        for field, variable in agent_shift.RUNTIME_ENV.items():
            self.assertEqual(runtime[field], overrides[variable])
        agent_shift.load_schedule(self.schedule, environ=overrides)
        with patch.dict(os.environ, overrides):
            self.assertEqual(agent_shift.logs_dir(), pathlib.Path("/portable/logs"))
            command, env = agent_shift.session_command(self.config["kinds"]["weekly"], 12345, "ready", "rules")
        self.assertEqual(command[0], "/portable/claude")
        self.assertEqual((env["PATH"], env["SHELL"]), ("/portable/path", "/portable/shell"))

    def test_router_interpreter_port_and_notify_use_runtime_configuration(self):
        router = Mock()
        router.stdout.readline.return_value = "12345\n"
        with patch.object(agent_shift, "executable", return_value="present"), \
                patch.object(agent_shift.subprocess, "Popen", return_value=router) as start, \
                patch.object(agent_shift.select, "select", return_value=([router.stdout], [], [])):
            process, port = agent_shift.start_router(self.config["kinds"]["weekly"])
        self.assertEqual(start.call_args.args[0], ["/usr/bin/python3", str(self.root / "router.py")])
        self.assertEqual(start.call_args.kwargs["env"]["MODEL_ROUTER_PORT"], "9001")
        self.assertEqual(start.call_args.kwargs["env"]["PATH"], self.config["runtime"]["path"])
        self.assertEqual((process, port), (router, 12345))
        with patch.object(agent_shift.subprocess, "run") as notify:
            agent_shift.notify("test")
        self.assertEqual(notify.call_args.args[0], [str(self.root / "notify"), "notify", "test"])

    def test_missing_binary_or_router_records_failure_before_preflight(self):
        for field in ("claude", "router"):
            with self.subTest(field=field):
                if field == "router":
                    (self.root / "router.py").unlink()
                with patch.object(agent_shift, "executable", side_effect=lambda value, runtime: None if field == "claude" and value == runtime["claude"] else value), \
                        patch.object(agent_shift, "wait_for_github") as access, \
                        patch.object(agent_shift.subprocess, "Popen") as start, patch.object(agent_shift, "notify"):
                    result = agent_shift.run_shift("missing-" + field, 1)
                self.assertIn("runtime prerequisite missing: " + field, result["reason"])
                self.assertEqual(result["attempts"], [])
                self.assertTrue((agent_shift.logs_dir() / ("missing-" + field) / "summary.json").exists())
                access.assert_not_called()
                start.assert_not_called()
                self.assertFalse((self.root / shiftlock.SIDECAR).exists())

    def test_doctor_is_read_only_and_discards_auth_output(self):
        def command(argv, **options):
            if argv == ["gh", "auth", "status"]:
                self.assertEqual(options["stdout"], agent_shift.subprocess.DEVNULL)
                self.assertEqual(options["stderr"], agent_shift.subprocess.DEVNULL)
                return Mock(returncode=0, stdout="secret fixture", stderr="secret fixture")
            output = "Python 3.12.8" if argv[-1] == "--version" else "main\n" if "branch" in argv else ""
            return Mock(returncode=0, stdout=output)
        before = set(self.root.rglob("*"))
        with patch.object(agent_shift, "load_schedule", return_value=self.config), \
                patch.object(agent_shift, "executable", return_value="present"), \
                patch.object(agent_shift.subprocess, "run", side_effect=command), \
                patch.object(agent_shift, "notify") as notify, patch.object(agent_shift.subprocess, "Popen") as launch:
            report = agent_shift.doctor()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["checks"]["gh_auth"], {"status": "ok", "exit_code": 0})
        self.assertNotIn("secret fixture", json.dumps(report))
        self.assertEqual(set(self.root.rglob("*")), before)
        self.assertFalse(pathlib.Path(self.config["runtime"]["logs"]).exists())
        notify.assert_not_called()
        launch.assert_not_called()

    def test_inspection_commands_leave_a_fresh_checkout_unchanged(self):
        sources = ("factory/__init__.py", "factory/core/__init__.py", "factory/util.py",
                   "factory/core/privacy.py", "factory/core/shiftlock.py", "schedule/agent-shift.py")
        for action, expected_exit in (("plan", 0), ("doctor", 1)):
            with self.subTest(action=action):
                checkout = self.root / action
                for name in sources:
                    target = checkout / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / name, target)
                config = copy.deepcopy(self.config)
                config["runtime"].update(path=str(checkout / "no-tools"), python=sys.executable,
                                         logs=str(checkout / "logs"))
                (checkout / "schedule/schedule.json").write_text(json.dumps(config))
                (checkout / "factory.config.json").write_text(json.dumps({"repos_dir": str(checkout / "repos"),
                                                                         "private_terms": []}))
                before = file_inventory(self.root)
                env = {"HOME": str(checkout / "home"), "PATH": str(checkout / "no-tools")}
                result = subprocess.run([sys.executable, str(checkout / "schedule/agent-shift.py"), action],
                                        cwd=checkout, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, expected_exit, result.stderr)
                report = json.loads(result.stdout)
                if action == "doctor":
                    self.assertFalse(report["ok"])
                    self.assertIsNone(report["checks"]["gh_auth"]["exit_code"])
                self.assertEqual(file_inventory(self.root), before)

    def test_doctor_reports_missing_tools_old_python_and_dirty_checkout(self):
        def command(argv, **options):
            output = "Python 3.11.9" if argv[-1] == "--version" else "topic\n" if "branch" in argv else " M changed.py\n"
            return Mock(returncode=0, stdout=output)
        with patch.object(agent_shift, "load_schedule", return_value=self.config), \
                patch.object(agent_shift, "executable", return_value=None), \
                patch.object(agent_shift.subprocess, "run", side_effect=command), \
                patch.object(agent_shift.os, "access", return_value=False):
            report = agent_shift.doctor()
        self.assertFalse(report["ok"])
        for field in ("git", "gh", "gitleaks", "gh_auth", "claude", "router", "router_python", "logs", "checkout"):
            self.assertEqual(report["checks"][field]["status"], "missing", field)
        self.assertEqual(report["checks"]["checkout"], {"status": "missing", "on_main": False, "clean": False})

    def test_doctor_reports_invalid_schedule_as_json(self):
        self.schedule.write_text('{"runtime": {"router_port": "invalid"}}')
        with patch.object(sys, "argv", ["agent-shift.py", "doctor"]), \
                patch.object(agent_shift, "load_schedule", side_effect=ValueError("invalid")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(agent_shift.main(), 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["checks"]["runtime"]["status"], "missing")
        self.assertEqual(report["checks"]["effective_schedule"]["status"], "missing")

    def test_doctor_reports_missing_zone_data_without_traceback(self):
        with patch.object(agent_shift, "ZoneInfo", side_effect=KeyError("missing zone data")), \
                patch.object(agent_shift, "load_schedule", side_effect=KeyError("missing zone data")), \
                patch.object(agent_shift, "executable", return_value=None), \
                patch.object(agent_shift.subprocess, "run", side_effect=OSError("missing tool")):
            report = agent_shift.doctor()
        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"]["zoneinfo"]["status"], "missing")
        self.assertEqual(report["checks"]["effective_schedule"]["status"], "missing")
        self.assertIsNone(report["checks"]["gh_auth"]["exit_code"])

    def test_daily_prompt_requires_inbox_triage_before_new_work(self):
        text = agent_shift.PROMPT.format(name="fixture-shift")
        for command in ("gh api --paginate", "bin/factory lanes", "bin/factory park", "bin/factory gh -- issue comment",
                        "bin/factory lessons add --status candidate", "docs/owner-actions.md"):
            self.assertIn(command, text)
        self.assertIn("no lane-stop command exists", text)
        self.assertIn("withhold all new contributions", text)
        self.assertLess(text.index("read all open issues"), text.index("qualified new contribution"))

    def test_isolated_logs_exclude_the_configured_production_tree(self):
        with patch.dict(os.environ, {"FACTORY_SHIFT_LOGS": str(self.root / "production/logs/check")}), \
                self.assertRaisesRegex(ValueError, "non-production"):
            agent_shift.require_isolated_logs()


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
            self.assertEqual(call.kwargs["env"]["PATH"], agent_shift.runtime_config()["path"])
            self.assertNotIn("GH_TOKEN", call.kwargs["env"])
            self.assertLessEqual(call.kwargs["timeout"], 30)

    def test_locked_keychain_and_timeout_are_not_bad_login_diagnoses(self):
        with patch.object(agent_shift.subprocess, "run", return_value=Mock(returncode=1, stderr="error -25293")):
            self.assertIn("keychain is locked", agent_shift.github_access(30))
        with patch.object(agent_shift.subprocess, "run", side_effect=agent_shift.subprocess.TimeoutExpired("gh", 30)):
            self.assertIn("timed out", agent_shift.github_access(30))


class Schedule(unittest.TestCase):
    def setUp(self):
        self.config = agent_shift.load_schedule(environ={})
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.now = dt.datetime.fromisoformat("2026-11-22T22:35:00+08:00")
        for mocker in (patch.object(shiftlock, "ROOT", self.root),
                       patch.object(shiftlock, "now", side_effect=lambda: self.now),
                       patch.object(agent_shift, "utcnow", side_effect=lambda: self.now),
                       patch.object(agent_shift, "require_runtime", side_effect=agent_shift.runtime_config),
                       patch.object(agent_shift, "notify")):
            mocker.start()
            self.addCleanup(mocker.stop)

    def claim(self, kind="weekly", **options):
        return agent_shift.claim(self.logs, kind, agent_shift.slot_at(self.now, self.config["kinds"][kind]), self.now, **options)

    def test_slots_do_not_follow_the_machine_zone(self):
        for zone in ("America/Los_Angeles", "UTC", "Asia/Tokyo"):
            with patch.dict(os.environ, {"TZ": zone}):
                result = agent_shift.plan(self.config, self.now)
                self.assertEqual(result["daily"]["next_slot"], "2026-11-23T21:43:00+08:00")
                self.assertEqual(result["weekly"]["next_slot"], "2026-11-29T22:30:00+08:00")
                self.assertEqual(agent_shift.plan(self.config, self.now.astimezone(dt.timezone.utc)), result)
        with self.assertRaisesRegex(ValueError, "offset"):
            agent_shift.plan(self.config, self.now.replace(tzinfo=None))

    def test_keys_use_slot_dates_across_midnight_and_iso_year(self):
        daily, weekly = self.config["kinds"].values()
        for instant, expected in (("2026-11-23T00:10:00+08:00", "daily-2026-11-22"),
                                  ("2026-11-22T23:59:00-08:00", "daily-2026-11-22")):
            slot = agent_shift.slot_at(dt.datetime.fromisoformat(instant), daily)
            self.assertEqual(agent_shift.run_key("daily", slot), expected)
        slot = agent_shift.slot_at(dt.datetime.fromisoformat("2027-01-04T01:00:00+08:00"), weekly)
        self.assertEqual(agent_shift.run_key("weekly", slot), "weekly-2026-W53")
        next_slot = agent_shift.slot_at(slot, weekly, upcoming=True)
        self.assertEqual(agent_shift.run_key("weekly", next_slot), "weekly-2027-W01")

    def test_claim_is_exclusive_and_force_preserves_the_original(self):
        with agent_shift.concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(lambda _: self.claim(), range(8)))
        self.assertEqual(sum(record is not None for record in claims), 1)
        original = next(record for record in claims if record)
        forced = agent_shift.claim(self.logs, "weekly", dt.datetime.fromisoformat(original["slot"]), self.now, force=True)
        self.assertNotEqual(original["run_key"], forced["run_key"])
        self.assertEqual(len(agent_shift.read_records(self.logs)), 2)

    def test_grace_is_inclusive_and_old_slots_are_skipped(self):
        for kind, spec in self.config["kinds"].items():
            slot = agent_shift.slot_at(self.now, spec)
            deadline = slot + dt.timedelta(minutes=spec["grace_minutes"])
            self.assertFalse(agent_shift.missed(slot, spec, deadline))
            self.assertTrue(agent_shift.missed(slot, spec, deadline + dt.timedelta(seconds=1)))
        record = self.claim()
        self.now += dt.timedelta(hours=9)
        with patch.object(agent_shift, "run_shift") as run:
            result = agent_shift.execute_claim(self.logs, record, self.config)
        self.assertEqual((result["status"], result["reason"]), ("skipped", "missed"))
        run.assert_not_called()
        self.assertTrue((self.logs / record["name"] / "summary.json").exists())

    def test_long_gap_selects_only_latest_slot_and_never_two_makeups(self):
        cursors = {kind: "2026-11-01T00:00:00+08:00" for kind in self.config["kinds"]}
        ready = agent_shift.scheduled_records(self.logs, self.config, self.now, cursors)
        self.assertEqual([r["run_key"] for r in ready], ["weekly-2026-W47"])
        records = agent_shift.read_records(self.logs)
        self.assertEqual(sum(r["status"] == "claimed" for r in records), 1)
        self.assertTrue(all(r["reason"].startswith("missed") for r in records if r["status"] == "skipped"))
        self.assertEqual(agent_shift.scheduled_records(self.logs, self.config, self.now, cursors), [])

    def test_weekly_waits_for_lease_then_runs_once_without_releasing_daily(self):
        record = self.claim()
        held = shiftlock.run("acquire", "auto-daily")
        with patch.object(agent_shift, "run_shift", return_value={"status": "exited 0", "reason": "", "ended_at": self.now.isoformat()}) as run:
            result = agent_shift.execute_claim(self.logs, record, self.config)
            self.assertEqual(result["status"], "claimed")
            self.assertEqual(shiftlock.run("status")["holder"], held["holder"])
            agent_shift.execute_claim(self.logs, record, self.config)
            run.assert_not_called()
            shiftlock.run("release", "auto-daily")
            agent_shift.execute_claim(self.logs, record, self.config)
            agent_shift.execute_claim(self.logs, record, self.config)
            run.assert_called_once()

    def test_only_daily_can_proceed_to_generation_checked_stale_recovery(self):
        owner = "auto-previous"
        held = shiftlock.run("acquire", owner)["holder"]
        previous = self.logs / owner
        previous.mkdir()
        (previous / "runner.lock").touch()
        self.assertIsNone(agent_shift.busy_reason(self.logs, recover_auto=True))
        self.assertEqual(agent_shift.busy_reason(self.logs), "shift held by " + owner)
        self.assertEqual(shiftlock.run("status")["holder"], held)
        with agent_shift.file_lock(previous / "runner.lock"):
            self.assertIn("runner alive", agent_shift.busy_reason(self.logs, recover_auto=True))

    def test_weekly_waits_for_runner_even_without_a_lease_and_skips_after_grace(self):
        record = self.claim()
        other = self.logs / "auto-daily"
        other.mkdir()
        with agent_shift.file_lock(other / "runner.lock"), patch.object(agent_shift, "run_shift") as run:
            self.assertEqual(agent_shift.execute_claim(self.logs, record, self.config)["status"], "claimed")
            self.now += dt.timedelta(hours=9)
            self.assertEqual(agent_shift.execute_claim(self.logs, record, self.config)["reason"], "missed")
            run.assert_not_called()
        self.assertFalse((self.root / shiftlock.SIDECAR).exists())

    def test_kill_switch_and_per_kind_disable_block_launch(self):
        config = agent_shift.load_schedule(environ={"FACTORY_SHIFT_DISABLED": "1"})
        self.assertFalse(any(row["enabled"] for row in agent_shift.plan(config, self.now).values()))
        with patch.object(agent_shift, "run_shift") as run:
            result = agent_shift.execute_claim(self.logs, self.claim(), config)
        self.assertEqual(result["reason"], "disabled")
        run.assert_not_called()
        self.config["enabled"] = False
        self.assertFalse(agent_shift.enabled(self.config, "daily"))
        self.config["enabled"] = True
        self.config["kinds"]["weekly"]["enabled"] = False
        self.assertTrue(agent_shift.enabled(self.config, "daily"))
        self.assertFalse(agent_shift.enabled(self.config, "weekly"))

    def test_removed_environment_overrides_fail_instead_of_disappearing(self):
        for variable in ("FACTORY_SHIFT_MODEL", "FACTORY_SHIFT_TIMES", "FACTORY_SHIFT_MINUTES"):
            with self.assertRaisesRegex(ValueError, variable):
                agent_shift.load_schedule(environ={variable: "obsolete"})
        with self.assertRaises(ValueError):
            agent_shift.load_schedule(environ={"FACTORY_SHIFT_DISABLED": "maybe"})

    def test_routing_separates_main_subagent_and_compaction_models(self):
        for kind, model in (("daily", "opus"), ("weekly", "opus")):
            spec = self.config["kinds"][kind]
            command, env = agent_shift.session_command(spec, 12345, "ready", "rules")
            self.assertEqual(command[command.index("--model") + 1], model)
            self.assertEqual(env["ANTHROPIC_MODEL"], model)
            for variable in ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
                self.assertEqual(env[variable], "sonnet")
            self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "haiku")
            self.assertIn("--forward-subagent-text", command)
            self.assertEqual(env["TZ"], "Asia/Singapore")

    def test_abandoned_run_is_failed_without_relaunch_and_locked_run_is_left_alone(self):
        record = self.claim("daily")
        agent_shift.transition(self.logs, record, "running")
        with agent_shift.file_lock(self.logs / "runs" / (record["run_key"] + ".lock")):
            agent_shift.recover_abandoned(self.logs, self.config, self.now)
            self.assertEqual(agent_shift.read_records(self.logs)[0]["status"], "running")
        agent_shift.recover_abandoned(self.logs, self.config, self.now)
        self.assertEqual(agent_shift.read_records(self.logs)[0]["status"], "failed")
        self.assertIsNone(self.claim("daily"))
        self.assertFalse((self.root / shiftlock.SIDECAR).exists())

    def test_deferred_record_survives_restart_and_is_not_reclaimed(self):
        record = self.claim()
        agent_shift.defer_record(self.logs, record, self.config["kinds"]["weekly"], "daily")
        restored = agent_shift.read_records(self.logs)[0]
        self.assertIsNone(self.claim())
        agent_shift.recover_abandoned(self.logs, self.config, self.now)
        with patch.object(agent_shift, "run_shift", return_value={"status": "exited 0", "reason": "", "ended_at": self.now.isoformat()}) as run:
            agent_shift.execute_claim(self.logs, restored, self.config)
            agent_shift.execute_claim(self.logs, record, self.config)
        run.assert_called_once()

    def test_pending_expiration_never_overwrites_a_running_manual_key(self):
        record = self.claim()
        agent_shift.transition(self.logs, record, "running")
        self.now += dt.timedelta(hours=9)
        agent_shift.hold_pending(self.logs, record, self.config, self.now)
        self.assertEqual(agent_shift.read_records(self.logs)[0]["status"], "running")

    def test_router_failure_records_summary_and_notification_without_session(self):
        record = self.claim("daily", dry_run=True)
        with patch.object(agent_shift, "start_router", side_effect=ValueError("router unavailable")), \
                patch.object(agent_shift, "run_shift") as run:
            result = agent_shift.execute_claim(self.logs, record, self.config, dry_run=True)
        self.assertEqual((result["status"], result["reason"]), ("failed", "router unavailable"))
        self.assertEqual(agent_shift.read_records(self.logs, dry_run=True)[0]["status"], "failed")
        agent_shift.notify.assert_called()
        run.assert_not_called()

    def test_probe_limits_tools_and_turns_without_permission_bypass(self):
        command, env = agent_shift.session_command(self.config["kinds"]["daily"], 12345, "ready", "", probe=True)
        self.assertEqual(command[command.index("--tools") + 1], "Agent")
        self.assertEqual(command[command.index("--max-turns") + 1], "4")
        self.assertIn("--safe-mode", command)
        self.assertIn("--no-session-persistence", command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertNotIn("CLAUDECODE", env)

    def test_dry_run_routes_under_locks_without_launch_or_production_state(self):
        record = self.claim("daily", dry_run=True)
        self.now += dt.timedelta(days=3)
        def router(spec):
            for path in (self.logs / "active.lock", self.logs / record["name"] / "runner.lock"):
                with self.assertRaises(BlockingIOError), agent_shift.file_lock(path):
                    pass
            return Mock(), 12345
        with patch.object(agent_shift, "start_router", side_effect=router), patch.object(agent_shift, "close_router"), \
                patch.object(agent_shift, "run_shift") as run, patch.object(agent_shift.subprocess, "Popen") as launch:
            result = agent_shift.execute_claim(self.logs, record, self.config, dry_run=True)
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["models"], {"main": [], "subagent": []})
        self.assertFalse((self.root / shiftlock.SIDECAR).exists())
        run.assert_not_called()
        launch.assert_not_called()
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "non-production"):
            agent_shift.require_isolated_logs()

    def test_dry_run_claims_cannot_be_resumed_by_the_daemon(self):
        def daemon_tick(seconds):
            for record in agent_shift.read_records(self.logs):
                if record["status"] == "claimed" and record.get("deferred_at"):
                    agent_shift.execute_claim(self.logs, record, self.config)

        with patch.dict(os.environ, {"FACTORY_SHIFT_LOGS": str(self.logs)}), \
                patch.object(agent_shift, "logs_dir", return_value=self.logs), \
                patch.object(agent_shift, "load_schedule", side_effect=lambda: copy.deepcopy(self.config)), \
                patch.object(agent_shift, "busy_reason", side_effect=["daily runner", None]), \
                patch.object(agent_shift, "start_router", return_value=(Mock(), 12345)), \
                patch.object(agent_shift, "close_router"), patch.object(agent_shift.time, "sleep", side_effect=daemon_tick), \
                patch.object(agent_shift, "run_shift", return_value={"status": "exited 0", "reason": "", "ended_at": self.now.isoformat()}) as run, \
                patch.object(sys, "argv", ["agent-shift.py", "run", "--kind", "weekly", "--dry-run"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent_shift.main(), 0)
        run.assert_not_called()
        self.assertIsNotNone(self.claim())
        paths = list((self.logs / "dry-run" / "runs").glob("*.json"))
        self.assertEqual(len(paths), 1)
        record = json.loads(paths[0].read_text())
        self.assertTrue(record["dry_run"])
        self.assertEqual(record["status"], "finished")
        self.assertTrue(record["run_key"].startswith("dry-run-weekly-"))
        self.assertTrue(record["deferred_at"])

    def test_daemon_resumes_only_production_claims_with_the_saved_minutes(self):
        record = self.claim(minutes=1)
        verification = self.claim(dry_run=True)
        for entry in (record, verification):
            agent_shift.defer_record(self.logs, entry, self.config["kinds"]["weekly"], "daily", entry["dry_run"])
        agent_shift.atomic_json(self.logs / "scheduler-state.json", {kind: self.now.isoformat() for kind in self.config["kinds"]})
        held = shiftlock.run("acquire", "interactive-daily")["holder"]
        ticks = []
        class EndLoop(Exception):
            pass
        class ImmediatePool:
            def __init__(self, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def submit(self, function, *args):
                result = agent_shift.concurrent.futures.Future()
                result.set_result(function(*args))
                return result
        def tick(seconds):
            ticks.append(seconds)
            if len(ticks) == 1:
                self.assertEqual(shiftlock.run("status")["holder"], held)
                self.assertTrue(shiftlock.run("release", "interactive-daily")["ok"])
            if len(ticks) == 3:
                raise EndLoop()
        with patch.object(agent_shift, "logs_dir", return_value=self.logs), \
                patch.object(agent_shift, "load_schedule", side_effect=lambda: copy.deepcopy(self.config)), \
                patch.object(agent_shift.concurrent.futures, "ThreadPoolExecutor", ImmediatePool), \
                patch.object(agent_shift.time, "sleep", side_effect=tick), \
                patch.object(agent_shift, "run_shift", return_value={"status": "exited 0", "reason": "", "ended_at": self.now.isoformat()}) as run:
            with self.assertRaises(EndLoop):
                agent_shift.daemon()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[1], 1)
        self.assertEqual(run.call_args.args[4], record["run_key"])
        self.assertEqual(agent_shift.read_records(self.logs, dry_run=True)[0]["status"], "claimed")
        heartbeat = json.loads((self.logs / "scheduler.json").read_text())
        self.assertEqual(heartbeat["kinds"]["weekly"]["last_key"], record["run_key"])
        self.assertEqual(heartbeat["kinds"]["weekly"]["last_status"], "finished")

    def test_completed_dry_run_does_not_consume_the_production_key(self):
        with patch.dict(os.environ, {"FACTORY_SHIFT_LOGS": str(self.logs)}), \
                patch.object(agent_shift, "logs_dir", return_value=self.logs), \
                patch.object(agent_shift, "start_router", return_value=(Mock(), 12345)), \
                patch.object(agent_shift, "close_router"), \
                patch.object(sys, "argv", ["agent-shift.py", "run", "--kind", "daily", "--dry-run"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent_shift.main(), 0)
            self.assertEqual(agent_shift.main(), 0)
        self.assertIsNotNone(self.claim("daily"))

    def test_verification_mode_is_checked_even_if_a_record_is_misplaced(self):
        record = self.claim()
        agent_shift.transition(self.logs, record, "claimed", dry_run=True, deferred_at=self.now.isoformat())
        with patch.object(agent_shift, "run_shift", return_value={"status": "exited 0", "reason": "", "ended_at": self.now.isoformat()}) as run:
            with self.assertRaisesRegex(ValueError, "execution mode"):
                agent_shift.execute_claim(self.logs, record, self.config)
        run.assert_not_called()
        self.assertEqual(agent_shift.read_records(self.logs), [])
        self.assertIsNone(agent_shift.heartbeat(self.config, self.logs, self.now)["kinds"]["weekly"]["last_key"])

    def test_isolation_rejects_production_descendants_and_custom_scheduler_roots(self):
        default = self.root / "production" / "shift-logs"
        default.mkdir(parents=True)
        alias = self.root / "alias"
        alias.symlink_to(default, target_is_directory=True)
        for target in (default, default / "diagnostics", alias / "diagnostics"):
            with self.subTest(target=target), \
                    patch.dict(os.environ, {"FACTORY_SHIFT_LOGS": str(target)}), \
                    patch.object(agent_shift, "path_from_config", return_value=default.parent / "clones"), \
                    self.assertRaisesRegex(ValueError, "non-production"):
                agent_shift.require_isolated_logs()
        before = set(self.logs.iterdir())
        with agent_shift.file_lock(self.logs / "scheduler.lock"):
            for target in (self.logs, self.logs / "diagnostics"):
                with self.subTest(target=target), patch.dict(os.environ, {"FACTORY_SHIFT_LOGS": str(target)}), \
                        self.assertRaisesRegex(ValueError, "non-production"):
                    agent_shift.require_isolated_logs()
        self.assertEqual(set(self.logs.iterdir()) - before, {self.logs / "scheduler.lock"})

    def assert_verification_rejects_link(self, action, mode, child):
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            base = pathlib.Path(temporary)
            production = base / "production/shift-logs"
            production.mkdir(parents=True)
            (production / "scheduler.json").write_text('{"production_fixture": true}\n')
            (production / "latest.json").write_text('{"preserve": true}\n')
            verification = base / "verification"
            verification.mkdir()
            target = verification / mode
            if child is not None:
                target.mkdir()
                target /= child
            destination = production
            if child in {"active.lock", "transcript.jsonl", "stderr.txt", "once"}:
                destination = production / "output"
                destination.write_text("preserve this output\n")
            target.symlink_to(destination, target_is_directory=destination.is_dir())
            before = file_inventory(base)
            argv = ["agent-shift.py", *action]
            with patch.dict(os.environ, {"FACTORY_SHIFT_LOGS": str(verification)}), \
                    patch.object(agent_shift, "path_from_config", return_value=production.parent / "repos"), \
                    patch.object(agent_shift, "start_router", return_value=(Mock(), 12345)) as router, \
                    patch.object(agent_shift, "close_router"), patch.object(agent_shift, "watch", return_value=0), \
                    patch.object(agent_shift.subprocess, "Popen") as launch, \
                    patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as stopped:
                    agent_shift.main()
                self.assertEqual(stopped.exception.code, 2)
            self.assertEqual(file_inventory(base), before)
            self.assertIn("non-production", error.getvalue())
            router.assert_not_called()
            launch.assert_not_called()

    def test_verification_rejects_mode_directory_symlinks_before_writes(self):
        for action, mode in ((["run", "--kind", "daily", "--dry-run"], "dry-run"),
                             (["probe", "--kind", "weekly"], "probe-weekly")):
            with self.subTest(mode=mode):
                self.assert_verification_rejects_link(action, mode, None)

    def test_verification_rejects_symlinked_state_and_output_descendants(self):
        for mode, children in (("dry-run", ("runs", "auto-dry-run-daily-2026-11-22", "active.lock")),
                               ("probe-weekly", ("transcript.jsonl", "stderr.txt", "once"))):
            action = ["run", "--kind", "daily", "--dry-run"] if mode == "dry-run" else ["probe", "--kind", "weekly"]
            for child in children:
                with self.subTest(mode=mode, child=child):
                    self.assert_verification_rejects_link(action, mode, child)

    def test_disable_during_preflight_skips_without_launch_or_publication(self):
        for kind in ("daily", "weekly"):
            for scope in ("global", "kind", "environment"):
                with self.subTest(kind=kind, scope=scope):
                    config_path = self.root / "schedule.json"
                    config_path.write_text(json.dumps(self.config))
                    load = agent_shift.load_schedule
                    def preflight(seconds):
                        config = copy.deepcopy(self.config)
                        if scope == "environment":
                            os.environ["FACTORY_SHIFT_DISABLED"] = "1"
                        elif scope == "global":
                            config["enabled"] = False
                        else:
                            config["kinds"][kind]["enabled"] = False
                        config_path.write_text(json.dumps(config))
                        return {"ok": True}
                    record = agent_shift.claim(self.logs, kind, agent_shift.slot_at(self.now, self.config["kinds"][kind]), self.now, force=True)
                    with patch.dict(os.environ, {"FACTORY_SHIFT_DISABLED": "0"}), \
                            patch.object(agent_shift, "logs_dir", return_value=self.logs), \
                            patch.object(agent_shift, "load_schedule", side_effect=lambda: load(config_path)), \
                            patch.object(agent_shift, "wait_for_github", side_effect=preflight), \
                            patch.object(agent_shift, "run_session", return_value=("exited 0", [0])) as launch, \
                            patch.object(agent_shift, "refresh_public", return_value={"ok": True}) as publish:
                        result = agent_shift.execute_claim(self.logs, record, self.config)
                    self.assertEqual(result["reason"], "disabled")
                    self.assertEqual(record["status"], "skipped")
                    launch.assert_not_called()
                    publish.assert_not_called()

    def test_lease_acquired_during_preflight_defers_then_launches_once(self):
        record = self.claim()
        holder = []
        def preflight(seconds):
            holder.append(shiftlock.run("acquire", "interactive-daily")["holder"])
            return {"ok": True}
        with patch.object(agent_shift, "logs_dir", return_value=self.logs), \
                patch.object(agent_shift, "wait_for_github", side_effect=preflight) as access, \
                patch.object(agent_shift, "run_session", return_value=("exited 0", [0])) as launch:
            result = agent_shift.execute_claim(self.logs, record, self.config)
            self.assertEqual(result["status"], "claimed")
            self.assertTrue(result["deferred_at"])
            self.assertEqual(shiftlock.run("status")["holder"], holder[0])
            launch.assert_not_called()
            with agent_shift.file_lock(self.logs / "active.lock"), \
                    agent_shift.file_lock(self.logs / record["name"] / "runner.lock"):
                pass
            self.assertTrue(shiftlock.run("release", "interactive-daily")["ok"])
            access.side_effect = None
            access.return_value = {"ok": True}
            restored = agent_shift.read_records(self.logs)[0]
            self.assertEqual(agent_shift.execute_claim(self.logs, restored, self.config)["status"], "exited 0")
            self.assertEqual(agent_shift.execute_claim(self.logs, restored, self.config)["status"], "already claimed")
            launch.assert_called_once()

    def test_repeated_preflight_contention_stays_deferred_until_grace_expires(self):
        record = self.claim(minutes=1)
        def preflight(seconds):
            self.assertEqual(seconds, 60)
            self.assertTrue(shiftlock.run("acquire", "interactive-daily")["ok"])
            return {"ok": True}
        with patch.object(agent_shift, "logs_dir", return_value=self.logs), \
                patch.object(agent_shift, "wait_for_github", side_effect=preflight), \
                patch.object(agent_shift, "run_session") as launch:
            for _ in range(2):
                self.assertEqual(agent_shift.execute_claim(self.logs, record, self.config)["status"], "claimed")
                self.assertTrue(shiftlock.run("release", "interactive-daily")["ok"])
            self.now += dt.timedelta(hours=9)
            result = agent_shift.execute_claim(self.logs, record, self.config)
        self.assertEqual((result["status"], result["reason"]), ("skipped", "missed"))
        launch.assert_not_called()

    def test_deferred_manual_minutes_survive_reload_and_restart(self):
        def stop_wait(seconds):
            raise InterruptedError("manual wait interrupted")
        for restart in (False, True):
            with self.subTest(restart=restart):
                with patch.object(agent_shift, "logs_dir", return_value=self.logs), \
                        patch.object(agent_shift, "load_schedule", side_effect=lambda: copy.deepcopy(self.config)), \
                        patch.object(agent_shift, "busy_reason", side_effect=["daily runner", None]), \
                        patch.object(agent_shift.time, "sleep", side_effect=stop_wait if restart else None), \
                        patch.object(agent_shift, "run_shift", return_value={"status": "exited 0", "reason": "", "ended_at": self.now.isoformat()}) as run, \
                        patch.object(sys, "argv", ["agent-shift.py", "run", "--kind", "weekly", "--minutes", "1", "--force-key"]), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    if restart:
                        with self.assertRaises(SystemExit):
                            agent_shift.main()
                        record = next(r for r in agent_shift.read_records(self.logs) if r["status"] == "claimed")
                        agent_shift.execute_claim(self.logs, record, copy.deepcopy(self.config))
                    else:
                        self.assertEqual(agent_shift.main(), 0)
                run.assert_called_once()
                self.assertEqual(run.call_args.args[1], 1)
                self.assertEqual(run.call_args.args[2]["minutes"], 1)
                self.assertEqual(self.config["kinds"]["weekly"]["minutes"], 180)

    def test_plan_does_not_write_and_heartbeat_has_per_kind_status(self):
        before = list(self.logs.iterdir())
        agent_shift.plan(self.config, self.now)
        self.assertEqual(list(self.logs.iterdir()), before)
        record = self.claim()
        result = agent_shift.heartbeat(self.config, self.logs, self.now)
        self.assertEqual(result["kinds"]["weekly"]["last_key"], record["run_key"])
        self.assertEqual(result["kinds"]["weekly"]["last_status"], "claimed")
        for kind in ("daily", "weekly"):
            self.assertTrue(result["kinds"][kind]["enabled"])
            self.assertTrue(result["kinds"][kind]["next_slot"].endswith("+08:00"))

    def test_scheduler_singleton_uses_flock_not_heartbeat_pid(self):
        (self.logs / "scheduler.json").write_text('{"pid": 0}')
        with agent_shift.file_lock(self.logs / "scheduler.lock"):
            with self.assertRaises(BlockingIOError), agent_shift.file_lock(self.logs / "scheduler.lock"):
                pass
        with agent_shift.file_lock(self.logs / "scheduler.lock"):
            pass

    def test_deadline_stops_the_process_group_and_escalates_if_needed(self):
        session = Mock(pid=12345)
        session.wait.side_effect = [agent_shift.subprocess.TimeoutExpired("session", 20),
                                    agent_shift.subprocess.TimeoutExpired("session", 30), 0]
        with patch.object(agent_shift.time, "time", side_effect=[0, 2]), patch.object(agent_shift.os, "killpg") as kill:
            result = agent_shift.watch(session, self.logs / "absent.jsonl", deadline=1)
        self.assertEqual(result, "stopped at the time limit")
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(12345, agent_shift.signal.SIGTERM), (12345, agent_shift.signal.SIGKILL)])

    def test_models_are_observed_from_messages_never_system_config(self):
        path = self.logs / "transcript.jsonl"
        path.write_text('\n'.join(json.dumps(event) for event in [
            {"type": "system", "model": "requested"},
            {"type": "assistant", "message": {"model": "served-main"}},
            {"type": "assistant", "parent_tool_use_id": "a", "message": {"model": "served-child"}},
            {"type": "assistant", "message": {"model": "served-fallback"}},
            {"type": "assistant", "message": {"model": "<synthetic>"}}]))
        self.assertEqual(agent_shift.observed_models(path), {"main": ["served-main", "served-fallback"], "subagent": ["served-child"]})


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
