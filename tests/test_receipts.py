"""Signed command receipts and the mechanical checks that consume them."""
import argparse
import copy
import hashlib
import hmac
import importlib
import io
import json
import os
import pathlib
import re
import select
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import setup_module
from factory.core import checks, receipts
from factory.core.gate import Report
from factory.util import CONFIG, Item, write_json


def setUpModule():
    setup_module(unittest)


class ReceiptSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.clone = self.root / "clone"
        self.clone.mkdir()
        self.item_dir = self.root / "item"
        self.item_dir.mkdir()
        for context in (patch.object(receipts, "ROOT", self.root), patch.object(checks, "ROOT", self.root),
                        patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})):
            context.start()
            self.addCleanup(context.stop)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", CONFIG["git_name"])
        self.git("config", "user.email", CONFIG["git_email"])
        (self.clone / "source.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "contrib/fix")
        (self.clone / "source.txt").write_text("fixed\n")
        self.git("commit", "-q", "-am", "fix")
        write_json(self.item_dir / "item.json", {"schema": 2, "line": "oss", "repo": "example/repo",
                   "clone": str(self.clone), "branch": "contrib/fix", "stacked_on": "main"})
        self.item = Item(self.item_dir)
        self.argv = ["python3", "-c", "print('passed')"]

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.clone), *args], check=True, capture_output=True, text=True).stdout.strip()

    def command(self, argv=None, **kwargs):
        self.item.save()
        with redirect_stdout(io.StringIO()):
            return receipts.run_cmd(self.item, self.argv if argv is None else argv, **kwargs)

    def fixture(self, **fields):
        head = self.item.head_sha()
        return receipts.save(self.item, {"label": "test-unit", "argv": self.argv, "cwd": str(self.clone),
                             "cwd_head": head, "item_head": head, "head_before": head, "head_after": head,
                             "tracked_dirty_before": False, "tracked_dirty_after": False, "clean": True,
                             "exit": 0, "expect_fail": False,
                             "started": "2026-01-01T00:00:00+00:00", "duration_s": 1,
                             "log_sha256": hashlib.sha256(b"passed\n").hexdigest(), "log_tail": ["passed"],
                             "env": dict(receipts.ENV), **fields})

    def require(self, **fields):
        entry = {"label": "test-unit", "argv": self.argv, "cwd": ".", **fields}
        self.item.meta["required_checks"] = [entry]
        write_json(self.item.file("redgreen.json"), {"cmd": shlex.join(entry["argv"]), "cwd": entry["cwd"],
                   "ok": True, "head_sha": self.item.head_sha()})

    def verify(self):
        rep = Report()
        checks.verify_receipts(self.item, rep)
        return rep

    def changed(self, path, text="change\n"):
        dest = self.clone / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        self.git("add", ".")
        self.git("commit", "-q", "-m", "update files")

    def phoenix(self):
        self.item.meta["repo"] = "Arize-ai/phoenix"
        name = "lines/oss/repos/Arize-ai__phoenix.rules.json"
        dest = self.root / name
        dest.parent.mkdir(parents=True)
        dest.write_text((ROOT / name).read_text())
        return dest

    def companion_check(self):
        rep = Report()
        checks.changeset_required(self.item, rep)
        return rep


class ReceiptCommands(ReceiptSandbox):
    def test_round_trip_real_command_records_environment_output_and_heads(self):
        argv = ["python3", "-c", "import os, sys; print(os.environ['CI'], os.environ['HUSKY'], os.environ['LEFTHOOK'], flush=True); print('stderr', file=sys.stderr)"]
        with patch.dict(os.environ, {"CI": "0", "HUSKY": "1", "LEFTHOOK": "1"}), redirect_stdout(io.StringIO()) as output:
            receipt = receipts.run_cmd(self.item, argv, label="test-env")
        log = self.item.file("receipts") / (receipt["id"] + ".log")
        self.assertEqual(receipt["exit"], 0)
        self.assertEqual(receipt["argv"], argv)
        self.assertEqual(receipt["cwd"], str(self.clone))
        self.assertEqual(receipt["cwd_head"], self.item.head_sha())
        self.assertEqual(receipt["item_head"], self.item.head_sha())
        self.assertEqual(receipt["env"], receipts.ENV)
        self.assertEqual(output.getvalue(), "1 0 0\nstderr\n")
        self.assertEqual(log.read_text(), output.getvalue())
        self.assertEqual(receipt["log_sha256"], hashlib.sha256(log.read_bytes()).hexdigest())
        self.assertEqual(receipt["log_tail"], ["1 0 0", "stderr"])
        self.assertGreaterEqual(receipt["duration_s"], 0)
        self.assertEqual(receipts.get(self.item, receipt["id"]), receipt)
        self.assertEqual(receipts.load(self.item), [receipt])
        self.assertTrue(receipts.valid(receipt))
        self.assertRegex(receipt["id"], r"^\d{8}T\d{12}Z-[0-9a-f]{4}$")
        self.assertEqual(set(receipt), {"id", "label", "argv", "cwd", "cwd_head", "item_head", "exit", "expect_fail",
                                     "started", "duration_s", "log_sha256", "log_tail", "env", "mac", "argv_home",
                                     "head_before", "head_after", "tracked_dirty_before", "tracked_dirty_after",
                                     "clean", "untracked_before", "untracked_after", "untracked", "timeout"})
        self.assertEqual(receipt["head_before"], receipt["head_after"])
        self.assertFalse(receipt["tracked_dirty_before"])
        self.assertFalse(receipt["tracked_dirty_after"])
        self.assertTrue(receipt["clean"])

    def test_key_is_private_stable_and_signs_canonical_json(self):
        receipt = self.fixture()
        path = self.root / ".receipt-key"
        key = path.read_bytes()
        self.assertEqual(len(key), 32)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        fields = {key: value for key, value in receipt.items() if key != "mac"}
        canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.assertEqual(receipt["mac"], hmac.new(key, canonical, hashlib.sha256).hexdigest())
        self.fixture()
        self.assertEqual(path.read_bytes(), key)
        self.assertTrue(receipts.valid(dict(reversed(list(receipt.items())))))

    def test_concurrent_first_use_keeps_one_signing_key(self):
        barrier = threading.Barrier(4)
        def signed(index):
            barrier.wait()
            return self.fixture(id=f"parallel-{index}")
        with ThreadPoolExecutor(max_workers=4) as pool:
            recorded = list(pool.map(signed, range(4)))
        self.assertEqual(len(receipts.load(self.item)), 4)
        self.assertTrue(all(receipts.valid(receipt) for receipt in recorded))
        self.assertEqual(len(list(self.root.glob(".receipt-key*"))), 1)

    def test_tampered_exit_is_invalid_and_cannot_be_rerun(self):
        receipt = self.command()
        receipt["exit"] = 8
        write_json(self.item.file("receipts") / (receipt["id"] + ".json"), receipt)
        self.assertFalse(receipts.valid(receipts.get(self.item, receipt["id"])))
        with patch.object(receipts, "run_cmd") as run_cmd, self.assertRaisesRegex(ValueError, "invalid"):
            receipts.rerun(self.item, receipt["id"])
        run_cmd.assert_not_called()

    def test_validation_does_not_create_a_key_or_accept_malformed_macs(self):
        self.assertFalse(receipts.valid({"id": "handwritten", "mac": "abc"}))
        self.assertFalse((self.root / ".receipt-key").exists())
        receipt = self.fixture()
        for mac in (None, [], "wrong", "é"):
            with self.subTest(mac=mac):
                self.assertFalse(receipts.valid({**receipt, "mac": mac}))
        (self.root / ".receipt-key").write_bytes(b"short")
        self.assertFalse(receipts.valid(receipt))

    def test_streams_output_to_log_before_the_command_finishes(self):
        marker = self.root / "finished"
        seen = []
        class Output(io.StringIO):
            def write(stream, text):
                if "started" in text:
                    logs = list(self.item.file("receipts").glob("*.log"))
                    seen.append((logs[0].read_text(), marker.exists()))
                return super().write(text)
        script = "import pathlib, time; print('started', flush=True); time.sleep(0.3); pathlib.Path('finished').write_text('done')"
        with redirect_stdout(Output()):
            receipts.run_cmd(self.item, ["python3", "-c", script], cwd=self.root)
        self.assertEqual(seen, [("started\n", False)])
        self.assertTrue(marker.exists())

    def test_timeout_kills_descendants_and_records_observed_output(self):
        marker = self.root / "survived"
        pid_file = self.root / "child.pid"
        child = ("import os, pathlib, time; pathlib.Path('child.pid').write_text(str(os.getpid())); "
                 "print('ready', flush=True); time.sleep(60); pathlib.Path('survived').write_text('bad')")
        parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)"
        capture = receipts._capture
        elapsed = []

        def cleanup_child():
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.addCleanup(cleanup_child)

        def after_ready(proc, log, timeout):
            self.assertTrue(select.select([proc.stdout], [], [], 10)[0], "descendant did not become ready")
            started = time.monotonic()
            result = capture(proc, log, timeout)
            elapsed.append(time.monotonic() - started)
            return result

        with patch.object(receipts, "_capture", side_effect=after_ready):
            receipt = self.command([sys.executable, "-c", parent], cwd=self.root, timeout=0.3, expect_fail=True)
        self.assertEqual(receipt["exit"], "timeout")
        self.assertIn("ready", receipt["log_tail"])
        self.assertTrue(receipts.valid(receipt))
        # Capture must reach EOF promptly even though the descendant inherited stdout.
        self.assertLess(elapsed[0], 3)
        # Returning from capture does not prove that the descendant was killed.
        deadline = time.monotonic() + 3
        while True:
            state = subprocess.run(["ps", "-p", pid_file.read_text(), "-o", "stat="],
                                   capture_output=True, text=True, timeout=3)
            self.assertIn(state.returncode, (0, 1), state.stderr)
            if not state.stdout.strip() or state.stdout.strip().startswith("Z"):
                pid_file.unlink()
                break
            self.assertLess(time.monotonic(), deadline, "descendant is still running after timeout")
            time.sleep(0.02)
        self.assertFalse(marker.exists())
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as status:
            receipts._result(self.item, receipt)
        self.assertEqual(status.exception.code, 1)

    def test_timeout_after_stdout_is_closed(self):
        receipt = self.command(["python3", "-c", "import os, time; os.close(1); os.close(2); time.sleep(60)"], timeout=0.15)
        self.assertEqual(receipt["exit"], "timeout")
        self.assertLess(receipt["duration_s"], 3)

    def test_empty_output_is_a_valid_proof(self):
        receipt = self.command(["python3", "-c", "pass"])
        self.assertEqual(receipt["log_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(receipt["log_tail"], [])
        self.assertTrue(receipts.valid(receipt))

    def test_cwd_head_is_separate_from_item_branch_head(self):
        base_dir = self.root / "base"
        self.git("worktree", "add", "--detach", str(base_dir), self.base)
        receipt = self.command(cwd=base_dir)
        self.assertEqual(receipt["cwd_head"], self.base)
        self.assertEqual(receipt["item_head"], self.item.head_sha())
        self.assertNotEqual(receipt["cwd_head"], receipt["item_head"])
        outside = self.command(cwd=self.root)
        self.assertIsNone(outside["cwd_head"])
        self.assertEqual(outside["item_head"], self.item.head_sha())

    def test_relative_cwd_is_resolved_from_clone(self):
        (self.clone / "package").mkdir()
        receipt = self.command(cwd="package")
        self.assertEqual(receipt["cwd"], str(self.clone / "package"))
        self.assertEqual(receipt["cwd_head"], self.item.head_sha())

    def test_default_cwd_does_not_repeat_a_relative_clone_path(self):
        self.item.meta["clone"] = os.path.relpath(self.clone)
        self.assertEqual(receipts.command_cwd(self.item), self.clone)
        self.assertEqual(receipts.command_cwd(self.item, "package"), self.clone / "package")

    def test_home_paths_are_scrubbed_and_rerun_restores_them(self):
        file = self.root / "data.txt"
        file.write_text("same")
        argv = ["python3", "-c", "import pathlib, sys; print(sys.argv[1]); print(pathlib.Path(sys.argv[1]).read_text())", str(file)]
        with patch.dict(os.environ, {"HOME": str(self.root)}):
            receipt = self.command(argv)
            self.assertEqual(receipt["cwd"], "~/clone")
            self.assertEqual(receipt["argv"][-1], "~/data.txt")
            self.assertEqual(receipt["log_tail"], ["~/data.txt", "same"])
            with redirect_stdout(io.StringIO()):
                repeated = receipts.rerun(self.item, receipt["id"])
        self.assertNotIn(str(self.root), json.dumps(receipt))
        self.assertEqual(repeated["match"], {"exit": True, "output": True})
        self.assertTrue(receipts.valid(repeated))

    def test_rerun_restores_a_home_scrubbed_from_the_middle_of_a_resolved_cwd(self):
        # A symlinked home appears inside the resolved clone path, not as its prefix
        home = "/" + self.root.name
        with patch.dict(os.environ, {"HOME": home}):
            receipt = self.command()
            self.assertEqual(receipt["cwd"], str(self.clone).replace(home, "~"))
            self.assertNotEqual(receipt["cwd"][:1], "~")
            self.assertEqual(receipts.command_cwd(self.item, receipt["cwd"]), self.clone)
            with redirect_stdout(io.StringIO()):
                repeated = receipts.rerun(self.item, receipt["id"])
        self.assertEqual(repeated["cwd"], receipt["cwd"])
        self.assertEqual(repeated["match"], {"exit": True, "output": True})

    def test_tail_keeps_only_last_forty_lines_and_four_hundred_characters(self):
        script = "for i in range(45): print(str(i) + ':' + 'é' * 450)"
        receipt = self.command(["python3", "-c", script])
        self.assertEqual(len(receipt["log_tail"]), 40)
        self.assertTrue(receipt["log_tail"][0].startswith("5:"))
        self.assertTrue(receipt["log_tail"][-1].startswith("44:"))
        self.assertTrue(all(len(line) == 400 for line in receipt["log_tail"]))

    def test_rerun_computes_exit_and_output_matches_independently(self):
        script = self.clone / "proof.py"
        script.write_text("print('before')\n")
        receipt = self.command(["python3", "proof.py"])
        with redirect_stdout(io.StringIO()):
            same = receipts.rerun(self.item, receipt["id"])
            script.write_text("print('after')\n")
            changed_output = receipts.rerun(self.item, receipt["id"])
            script.write_text("print('before')\nraise SystemExit(7)\n")
            changed_exit = receipts.rerun(self.item, receipt["id"])
        self.assertEqual(same["match"], {"exit": True, "output": True})
        self.assertEqual(changed_output["match"], {"exit": True, "output": False})
        self.assertEqual(changed_exit["match"], {"exit": False, "output": True})
        for repeated in (same, changed_output, changed_exit):
            self.assertNotEqual(repeated["id"], receipt["id"])
            self.assertEqual(repeated["rerun_of"], receipt["id"])
            self.assertEqual(receipts.get(self.item, repeated["id"]), repeated)
            self.assertTrue(receipts.valid(repeated))
        self.assertEqual([r["id"] for r in receipts.load(self.item)], sorted(r["id"] for r in receipts.load(self.item)))

    def test_argv_is_not_interpreted_by_a_shell(self):
        argv = ["python3", "-c", "import sys; print(sys.argv[1])", "one; two | three $HOME"]
        receipt = self.command(argv)
        self.assertEqual(receipt["log_tail"], [argv[-1]])
        with self.assertRaises(ValueError):
            self.command("python3 -c pass")
        with self.assertRaises(ValueError):
            self.command([])
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.command(timeout=timeout)

    def test_missing_and_malformed_receipts_do_not_count(self):
        self.assertIsNone(receipts.get(self.item, "missing"))
        self.assertEqual(receipts.load(self.item), [])
        receipt = self.fixture(id="second")
        self.fixture(id="first")
        self.item.file("receipts/broken.json").write_text("{")
        self.item.file("receipts/wrong.json").write_text(json.dumps(receipt))
        self.assertIsNone(receipts.get(self.item, "broken"))
        self.assertEqual([r["id"] for r in receipts.load(self.item)], ["first", "second"])
        self.assertIsNone(receipts.get(self.item, "../item"))
        self.assertIsNone(receipts.get(self.item, None))
        with self.assertRaises(ValueError):
            self.fixture(id="../item")


class MechanicalChecks(ReceiptSandbox):
    def test_python_short_version_and_help_receipts_are_not_required_checks(self):
        self.item.meta["no_test_reason"] = "Documentation-only change; no executable behavior."
        for flag, output in (("-V", "Python"), ("-h", "usage:")):
            with self.subTest(flag=flag):
                argv = ["python3", flag]
                receipt = self.command(argv)
                self.assertEqual(receipt["exit"], 0)
                self.assertTrue(receipts.valid(receipt))
                self.assertTrue(receipts.clean(receipt))
                log = self.item.file("receipts/" + receipt["id"] + ".log").read_text()
                self.assertIn(output, log)
                self.item.meta["required_checks"] = [{"label": "test-unit", "argv": argv, "cwd": "."}]
                report = self.verify()
                self.assertFalse(report.ok, report.text())
                self.assertIn("version or help", report.text())
                self.assertIn("replace it with the actual CI check", report.text())

    def test_clean_constant_or_version_receipts_do_not_replace_regression(self):
        self.changed("regression.py", "from pathlib import Path\nassert Path('source.txt').read_text() == 'fixed\\n'\n")
        regression = ["python3", "regression.py"]
        write_json(self.item.file("redgreen.json"), {"cmd": shlex.join(regression), "cwd": ".",
                   "ok": True, "head_sha": self.item.head_sha()})
        for argv in (["python3", "--version"], ["python3", "-c", "print('passed')"]):
            self.command(argv)
            self.item.meta["required_checks"] = [{"label": "test", "argv": argv, "cwd": "."}]
            self.assertFalse(self.verify().ok)
            self.assertIn("required_checks must include", self.verify().text())
        self.command(regression)
        self.item.meta["required_checks"].append({"label": "regression", "argv": regression, "cwd": "."})
        self.assertTrue(self.verify().ok, self.verify().text())
        self.item.file("redgreen.json").unlink()
        self.assertFalse(self.verify().ok)

    def test_verify_requires_every_command_regardless_of_label(self):
        self.require()
        self.fixture()
        self.assertTrue(self.verify().ok)
        lint = {"label": "lint", "argv": ["python3", "-m", "compileall", "."], "cwd": "."}
        self.item.meta["required_checks"].append(lint)
        self.assertFalse(self.verify().ok)
        self.fixture(label="lint", argv=lint["argv"])
        self.assertTrue(self.verify().ok)
        self.item.meta["required_checks"] = [lint]
        self.assertFalse(self.verify().ok)
        self.assertIn("required_checks must include", self.verify().text())

    def test_empty_or_malformed_required_checks_fail(self):
        for required in (None, [], "test", [{}], ["test"], [{"label": "test", "argv": "python3 -c pass"}],
                         [{"label": "test", "argv": [], "cwd": "."}], [{"label": "test", "argv": [None]}]):
            with self.subTest(required=required):
                self.item.meta["required_checks"] = required
                self.assertFalse(self.verify().ok)

    def test_invalid_failed_timed_out_stale_and_wrong_cwd_receipts_fail(self):
        self.require()
        for fields in ({"exit": 1}, {"exit": "timeout"}, {"exit": False}, {"cwd_head": self.base},
                       {"cwd_head": None}, {"cwd": str(self.root)}, {"argv": ["python3", "-c", "pass"]}):
            with self.subTest(fields=fields):
                receipt = self.fixture(**fields)
                self.assertFalse(self.verify().ok)
                self.item.file(f"receipts/{receipt['id']}.json").unlink()
        receipt = self.fixture()
        receipt["exit"] = 9
        write_json(self.item.file(f"receipts/{receipt['id']}.json"), receipt)
        self.assertFalse(self.verify().ok)
        receipt["exit"] = 0
        receipt["mac"] = "forged"
        write_json(self.item.file(f"receipts/{receipt['id']}.json"), receipt)
        self.assertFalse(self.verify().ok)

    def test_identical_recommit_keeps_receipt_valid_but_a_different_message_does_not(self):
        self.require()
        receipt = self.fixture()
        self.git("commit", "-q", "--allow-empty", "-m", "fix")
        self.assertNotEqual(self.item.head_sha(), receipt["cwd_head"])
        self.assertTrue(self.verify().ok, self.verify().text())
        self.git("commit", "-q", "--allow-empty", "-m", "changed message")
        self.assertFalse(self.verify().ok)

    def test_verify_matches_home_scrubbed_arguments_and_resolved_cwd(self):
        with patch.dict(os.environ, {"HOME": str(self.root)}):
            self.argv += [str(self.root / "argument")]
            self.require(cwd="~/clone")
            self.fixture()
            self.assertTrue(self.verify().ok)

    def test_changeset_is_required_for_phoenix_package_changes_on_legacy_items(self):
        self.phoenix()
        self.item.meta.pop("schema")
        self.changed("js/packages/client/src/lib.ts")
        rep = self.companion_check()
        self.assertFalse(rep.ok)
        self.assertIn("js/.changeset/*.md", rep.text())
        self.assertIn("L-045", rep.text())
        self.changed("js/.changeset/client-fix.md", "release note\n")
        self.assertTrue(self.companion_check().ok)

    def test_unrelated_and_nested_package_paths(self):
        self.phoenix()
        self.changed("docs/guide.txt")
        self.assertTrue(self.companion_check().ok)
        self.changed("js/packages/client/deep/path with spaces.ts")
        self.changed(".changeset/wrong-location.md")
        self.assertFalse(self.companion_check().ok)

    def test_changeset_rule_uses_the_repository_even_without_a_line_field(self):
        self.phoenix()
        self.changed("js/packages/client/index.ts")
        self.item.meta.pop("line")
        self.assertFalse(self.companion_check().ok)
        self.item.meta["repo"] = "example/repo"
        self.assertEqual(self.companion_check().rows, [])

    def test_malformed_rules_fail_instead_of_skipping_requirement(self):
        path = self.phoenix()
        for text in ("{", "[]", '{"requires":[{"changed":"js/**"}]}'):
            with self.subTest(text=text):
                path.write_text(text)
                self.assertFalse(self.companion_check().ok)

    def test_hook_environment_requires_baseline_only_when_global_hooks_are_set(self):
        for configured, baseline, ok in ((False, None, True), (True, None, False), (True, {}, False),
                                         (True, "not-a-baseline", False), (True, {"pre-commit": "digest"}, True)):
            with self.subTest(configured=configured, baseline=baseline):
                result = subprocess.CompletedProcess([], 0 if configured else 1, stdout="~/hooks\n" if configured else "")
                with patch.object(checks, "run", return_value=result), patch.dict(CONFIG, {"global_hooks_baseline": baseline}):
                    before = copy.deepcopy(CONFIG)
                    rep = Report()
                    checks.hook_env(self.item, rep)
                    self.assertEqual(rep.ok, ok)
                    self.assertEqual(CONFIG, before)

    def test_hook_config_read_failure_is_not_an_absent_hooks_path(self):
        with patch.object(checks, "run", return_value=subprocess.CompletedProcess([], 128, stdout="")):
            rep = Report()
            checks.hook_env(self.item, rep)
        self.assertFalse(rep.ok)

    def test_registered_checks_respect_schema_and_gate_mode(self):
        entries = {key: value for key, value in checks.registry().items() if key in ("verify-receipts", "changeset-required", "hook-env")}
        self.phoenix()
        self.changed("js/packages/client/index.ts")
        with patch.dict(checks._REGISTRY, entries, clear=True), patch.object(checks, "run", return_value=subprocess.CompletedProcess([], 0, stdout="~/hooks")), \
                patch.dict(CONFIG, {"global_hooks_baseline": None}):
            for schema in (1, 2):
                self.item.meta["schema"] = schema
                for mode in checks.MODES:
                    with self.subTest(schema=schema, mode=mode):
                        rep = Report()
                        checks.run_extra(self.item, rep, mode)
                        names = {row["check"] for row in rep.rows}
                        self.assertIn("changeset-required", names)
                        self.assertEqual("verify-receipts" in names, schema == 2 and mode != "prepare")
                        self.assertEqual("hook-env" in names, schema == 2 and mode == "release")
                        self.assertFalse(rep.ok)


class ReceiptCli(ReceiptSandbox):
    def parser(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        receipts.register(sub)
        checks.register(sub)
        return parser

    def invoke(self, *argv):
        args = self.parser().parse_args(argv)
        with redirect_stdout(io.StringIO()) as output:
            try:
                result = args.fn(args)
            except SystemExit as error:
                return error.code, output.getvalue()
        return result or 0, output.getvalue()

    def test_run_and_rerun_commands_and_listing(self):
        code, output = self.invoke("run", "--item", str(self.item_dir), "--label", "test-unit", "--", *self.argv)
        self.assertEqual(code, 0)
        original = receipts.load(self.item)[0]
        self.assertIn(original["id"], output)
        self.assertIn("exit=0", output)
        self.assertIn(original["id"] + ".log", output)
        code, output = self.invoke("rerun", "--item", str(self.item_dir), original["id"])
        self.assertEqual(code, 0)
        self.assertEqual(len(receipts.load(self.item)), 2)
        code, output = self.invoke("receipts", "--item", str(self.item_dir))
        self.assertEqual(code, 0)
        self.assertIn("id\tlabel\texit\thead\tvalid", output)
        self.assertIn("test-unit\t0\t", output)
        self.assertIn("\ttrue", output)

    def test_expected_failure_changes_cli_status_not_observed_exit(self):
        for exit_code, expect_fail, status in ((0, False, 0), (3, False, 1), (3, True, 0), (0, True, 1)):
            with self.subTest(exit_code=exit_code, expect_fail=expect_fail):
                flags = ["--expect-fail"] if expect_fail else []
                code, output = self.invoke("run", "--item", str(self.item_dir), *flags, "--", "python3", "-c", f"raise SystemExit({exit_code})")
                self.assertEqual(code, status)
                receipt = receipts.load(self.item)[-1]
                self.assertEqual(receipt["exit"], exit_code)
                self.assertEqual(receipt["expect_fail"], expect_fail)

    def test_checks_lists_legacy_and_registered_lessons(self):
        from factory.core import lessons

        with patch.object(lessons, "load", return_value=[{"id": "L-100", "enforced-by": "check:hygiene, check:verify-receipts — text"}]):
            code, output = self.invoke("checks")
        self.assertEqual(code, 0)
        self.assertIn("legacy\thygiene\tL-100\tfactory.core.slop.check", output)
        self.assertIn("registered\tverify-receipts\tL-043, L-051, L-100", output)
        self.assertIn("registered\tchangeset-required\tL-045", output)
        self.assertIn("registered\thook-env\tL-035", output)

    def test_legacy_index_has_exact_contract_ids_and_real_enforcers(self):
        expected = "gate-artifacts self-review user-impact independent-review review-head redgreen hygiene merges-cleanly issue-free outsiders-accepted ai-policy ai-disclosure issue-precondition issue-first owner-prose dco coordinator-review approval hooks-drift qualify policy-scan unresponsive-repo missing-lesson privacy-scan gitleaks commit-wrapper followup stop-check thread-reply clone-heal ship-preconditions health watch"
        self.assertEqual(set(checks.LEGACY), set(expected.split()))
        for id, description in checks.LEGACY.items():
            for name in re.findall(r"factory\.[\w.]+", description.partition(":")[0]):
                parts = name.split(".")
                module_parts = 2 if parts[1] == "util" else 3
                enforcer = importlib.import_module(".".join(parts[:module_parts]))
                for attribute in parts[module_parts:]:
                    enforcer = getattr(enforcer, attribute)
                self.assertTrue(callable(enforcer), (id, name))


class CapturedState(ReceiptSandbox):
    def test_dirty_tracked_input_never_proves_the_restored_commit(self):
        self.changed("proof.py", "raise SystemExit(7)\n")
        original = (self.clone / "proof.py").read_text()
        self.argv = ["python3", "proof.py"]
        self.require()
        (self.clone / "proof.py").write_text("raise SystemExit(0)\n")
        receipt = self.command()
        (self.clone / "proof.py").write_text(original)
        self.assertEqual(receipt["exit"], 0)
        self.assertFalse(self.verify().ok)
        self.assertIn("commit or revert", self.verify().text())
        self.assertTrue(receipt["tracked_dirty_before"])
        self.assertTrue(receipt["tracked_dirty_after"])
        self.assertFalse(receipt["clean"])

    def test_staged_and_post_command_changes_are_recorded(self):
        (self.clone / "source.txt").write_text("staged\n")
        self.git("add", "source.txt")
        staged = self.command()
        self.assertFalse(staged["clean"])
        self.assertTrue(staged["tracked_dirty_before"])
        self.git("commit", "-qm", "stage input")
        mutated = self.command(["python3", "-c", "from pathlib import Path; Path('source.txt').write_text('new')"])
        self.assertFalse(mutated["tracked_dirty_before"])
        self.assertTrue(mutated["tracked_dirty_after"])
        self.assertFalse(mutated["clean"])

    def test_head_movement_and_missing_repository_are_not_clean(self):
        real_capture = receipts._capture
        def advance(*args):
            exit_code = real_capture(*args)
            self.git("commit", "-q", "--allow-empty", "-m", "advance")
            return exit_code
        with patch.object(receipts, "_capture", side_effect=advance):
            moved = self.command()
        self.assertNotEqual(moved["head_before"], moved["head_after"])
        self.assertFalse(moved["clean"])
        self.assertFalse(self.command(cwd=self.root)["clean"])

    def test_untracked_files_are_counted_without_failing_proof(self):
        (self.clone / "untracked.txt").write_text("input")
        self.require()
        receipt = self.command()
        self.assertEqual(receipt["untracked"], 1)
        self.assertTrue(receipt["clean"])
        self.assertTrue(self.verify().ok)
        with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit):
            receipts._result(self.item, receipt)
        self.assertIn("untracked: 1", output.getvalue())

    def test_capture_fields_and_timeout_are_mac_protected(self):
        receipt = self.command(timeout=2)
        for field in ("head_before", "head_after", "tracked_dirty_before", "tracked_dirty_after",
                      "clean", "untracked", "timeout", "argv_home"):
            with self.subTest(field=field):
                changed = {**receipt, field: "changed"}
                self.assertFalse(receipts.valid(changed))

    def test_literal_tildes_and_scrubbed_paths_survive_the_same_argument(self):
        script = "import sys; print(sys.argv[1])"
        value = f"~/literal {self.root}/actual ~/other {self.root}/second"
        with patch.dict(os.environ, {"HOME": str(self.root)}):
            original = self.command(["python3", "-c", script, value, "~/only-literal"])
            with redirect_stdout(io.StringIO()):
                repeated = receipts.rerun(self.item, original["id"])
                again = receipts.rerun(self.item, repeated["id"])
        self.assertTrue(repeated["match"]["output"])
        self.assertTrue(again["match"]["output"])
        self.assertEqual(repeated["argv"], original["argv"])
        self.assertNotIn(str(self.root), json.dumps(original))

    def test_rerun_inherits_bounded_timeout_and_allows_override(self):
        original = self.command(["python3", "-c", "import time; time.sleep(0.2)"], timeout=0.02)
        with redirect_stdout(io.StringIO()):
            repeated = receipts.rerun(self.item, original["id"])
            longer = receipts.rerun(self.item, original["id"], timeout=2)
        self.assertEqual(repeated["exit"], "timeout")
        self.assertEqual(repeated["timeout"], 0.02)
        self.assertEqual(longer["exit"], 0)
        parser = argparse.ArgumentParser()
        receipts.register(parser.add_subparsers(required=True))
        args = parser.parse_args(["rerun", "--item", str(self.item.dir), original["id"], "--timeout", "3"])
        with patch.object(receipts, "rerun", return_value=longer) as run, redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            args.fn(args)
        self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_deleted_or_renamed_away_changeset_does_not_count(self):
        self.phoenix()
        self.changed("js/.changeset/old.md")
        self.git("branch", "companion-base")
        self.item.meta["stacked_on"] = "companion-base"
        self.changed("js/packages/client/index.ts")
        self.git("rm", "js/.changeset/old.md")
        self.git("commit", "-qm", "remove companion")
        self.assertFalse(self.companion_check().ok)
        self.changed("js/.changeset/new.md", "release note\n")
        self.assertTrue(self.companion_check().ok)
        self.git("mv", "js/.changeset/new.md", "release-note.txt")
        self.git("commit", "-qm", "move companion")
        self.assertFalse(self.companion_check().ok)

    def test_renaming_an_old_companion_does_not_add_a_release_note(self):
        self.phoenix()
        self.changed("js/.changeset/old.md")
        self.git("branch", "companion-base")
        self.item.meta["stacked_on"] = "companion-base"
        self.changed("js/packages/client/index.ts")
        self.git("mv", "js/.changeset/old.md", "js/.changeset/renamed.md")
        self.git("commit", "-qm", "rename old note")
        self.assertFalse(self.companion_check().ok)

    def test_modified_companion_exists_in_head_even_if_missing_locally(self):
        self.phoenix()
        self.changed("js/.changeset/old.md")
        self.git("branch", "companion-base")
        self.item.meta["stacked_on"] = "companion-base"
        self.changed("js/packages/client/index.ts")
        self.changed("js/.changeset/old.md", "new note\n")
        (self.clone / "js/.changeset/old.md").unlink()
        self.assertTrue(self.companion_check().ok)


if __name__ == "__main__":
    unittest.main()
