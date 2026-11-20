import io
import json
import pathlib
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import setup_module


def setUpModule():
    setup_module(unittest)


class CheckRegistration(unittest.TestCase):
    def test_gate_loads_review_checks_without_cli_import(self):
        script = textwrap.dedent('''
            import json
            import pathlib
            import subprocess
            import sys
            import tempfile
            from unittest.mock import patch

            sys.path.insert(0, str(pathlib.Path.cwd() / "tests"))
            from support import isolated_config
            from factory.core import gate
            from factory.util import Item

            assert "factory.cli" not in sys.modules
            assert "factory.core.review" not in sys.modules
            with isolated_config(), tempfile.TemporaryDirectory() as folder:
                root = pathlib.Path(folder)
                (root / "item.json").write_text(json.dumps({
                    "schema": 2, "repo": "example/repo", "clone": folder,
                }))
                item = Item(root)
                with patch.object(gate, "git"), patch.object(gate, "core_checks"), \\
                        patch.object(Item, "head_sha", return_value="a" * 40), \\
                        patch.object(gate.checks, "run", return_value=subprocess.CompletedProcess([], 1, stdout="")):
                    results = []
                    for _ in range(2):
                        results.append({mode: gate.run(item, mode=mode)[0].rows
                                        for mode in ("prepare", "review", "release")})
                assert "factory.cli" not in sys.modules
                print(json.dumps(results))
        ''')
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = {"prepare": {"demand-evidence", "value-review"},
                    "review": {"demand-evidence", "value-review", "review-obligations"},
                    "release": {"demand-evidence", "value-review", "review-obligations", "coordinator-proof"}}
        for attempt in json.loads(result.stdout):
            for mode, names in expected.items():
                with self.subTest(mode=mode):
                    rows = [row for row in attempt[mode] if row["check"] in expected["release"]]
                    self.assertEqual({row["check"] for row in rows}, names)
                    self.assertEqual(len(rows), len(names))
                    self.assertTrue(all(not row["ok"] for row in rows))

    def test_watch_help_includes_scanner_limits_and_freshness(self):
        from factory import cli
        from factory.oss import watch

        with patch.object(watch, "run") as scan, redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit:
                cli.main(["watch", "--help"])
        self.assertEqual(exit.exception.code, 0)
        self.assertIn(watch.HELP, " ".join(output.getvalue().split()))
        scan.assert_not_called()


class RequiredCommands(unittest.TestCase):
    def verify(self, argv, label="regression", redgreen=None, **meta):
        from factory.core import checks, gate, receipts, verdicts
        from factory.util import Item

        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder).resolve()
            (root / "item.json").write_text(json.dumps({"schema": 2, "repo": "example/repo", "clone": str(root),
                "required_checks": [{"label": label, "argv": argv, "cwd": "."}], **meta}))
            (root / "redgreen.json").write_text(json.dumps({"ok": True, "head_sha": "a" * 40,
                "cmd": shlex.join(argv), "cwd": ".", **(redgreen or {})}))
            item = Item(root)
            receipt = {"argv": argv, "cwd": str(root), "cwd_head": "a" * 40, "exit": 0,
                       "head_before": "a" * 40, "head_after": "a" * 40,
                       "tracked_dirty_before": False, "tracked_dirty_after": False, "clean": True}
            with patch.object(receipts, "load", return_value=[receipt]), patch.object(receipts, "valid", return_value=True), \
                    patch.object(Item, "head_sha", return_value="a" * 40), \
                    patch.object(verdicts, "bound", side_effect=lambda item, head, sha: bool(sha) and head.startswith(sha)):
                report = gate.Report()
                checks.verify_receipts(item, report)
                return report

    def test_noop_required_commands_are_refused_even_with_test_label(self):
        commands = [["true"], ["/usr/bin/true"], [":"], ["echo", "passed"], ["exit", "0"],
                    ["test", "1", "=", "1"], ["[", "1", "-eq", "1", "]"], ["test", "-n", "constant"],
                    ["bash", "-lc", ""], ["bash", "-lc", "# no checks\n"],
                    ["bash", "-lc", "true; : && echo passed; exit 0"],
                    ["bash", "-lc", "test 1 = 1\n[ x = x ]"],
                    ["bash", "-lc", "true # ignored; python -m unittest\n: # done"], [""]]
        for argv in commands:
            with self.subTest(argv=argv):
                report = self.verify(argv, "test-unit")
                self.assertFalse(report.ok, report.text())
                self.assertIn("cannot test", report.text())

    def test_real_commands_are_not_inferred_from_the_label(self):
        for argv in (["python", "-m", "unittest"], ["npm", "test"],
                     ["bash", "-lc", "echo starting; python -m unittest"],
                     ["bash", "-lc", "echo starting # comment\npython -m unittest"],
                     ["test", "-f", "dist/result.txt"], ["bash", "-lc", 'test "$result" = expected']):
            with self.subTest(argv=argv):
                self.assertTrue(self.verify(argv).ok)

    def test_version_or_help_only_commands_cannot_test_code(self):
        from factory.core import checks

        for argv in (["python", "--version"], ["/usr/bin/tool", "--help"], ["tool", "--version", "--help"],
                     ["bash", "-lc", "python --version && tool --help"]):
            with self.subTest(argv=argv):
                self.assertIsNotNone(checks.noop_reason(argv))
                self.assertFalse(self.verify(argv).ok)
        self.assertIsNone(checks.noop_reason(["tool", "--help", "input.txt"]))

    def test_known_tools_reject_short_version_and_help_commands(self):
        from factory.core import checks

        tools = ("python", "python3", "python3.14", "pypy3", "node", "npm", "pnpm", "yarn",
                 "bun", "deno", "git", "go", "cargo", "rustc", "ruby", "java", "tsc")
        flags = ("-V", "-v", "-h", "-?", "-version", "-help", "--version", "--help", "version", "help")
        accepted = []
        for tool in tools:
            for flag in flags:
                argv = ["/usr/bin/" + tool, flag]
                if checks.noop_reason(argv) is None:
                    accepted.append(argv)
        self.assertEqual(accepted, [])
        self.assertIsNotNone(checks.noop_reason(["node", "-v"]))
        self.assertIsNotNone(checks.noop_reason(["git", "version"]))
        self.assertIsNotNone(checks.noop_reason(["python3", "-V", "--help", "help"]))
        self.assertIsNotNone(checks.noop_reason(["bash", "-lc", "python3 -V && git version"]))

    def test_python_interpreter_names_reject_short_version_and_help(self):
        from factory.core import checks

        for tool in ("python", "python3", "python3.14", "python3.14t", "python3.14td", "python2.7", "pypy", "pypy3", "pypy3.10"):
            for executable in (tool, "/usr/bin/" + tool):
                for flag in ("-V", "-h"):
                    argv = [executable, flag]
                    with self.subTest(argv=argv):
                        self.assertIsNotNone(checks.noop_reason(argv))
                        report = self.verify(argv, "test-unit", no_test_reason="Documentation-only change; no executable behavior.")
                        self.assertFalse(report.ok, report.text())
                        self.assertIn("version or help flags alone cannot test code", report.text())

    def test_known_tool_check_arguments_are_not_version_or_help_only(self):
        from factory.core import checks

        for argv in (["python3", "-m", "unittest", "-v"], ["python3"], ["npm", "test"],
                     ["python3.14t", "-m", "unittest", "-v"], ["pypy3.10", "-m", "unittest"],
                     ["pythonista", "-V"], ["./python-tests", "-v"],
                     ["python3.14-custom", "-v"], ["python3.", "-h"], ["git", "status"]):
            with self.subTest(argv=argv):
                self.assertIsNone(checks.noop_reason(argv))
                self.assertTrue(self.verify(argv).ok)

    def test_short_flags_do_not_imply_version_or_help(self):
        from factory.core import checks

        for flag in ("-v", "-V", "-h", "-?", "-version", "-help", "version", "help"):
            for argv in (["./run-tests", flag], ["bash", "-lc", "./run-tests " + shlex.quote(flag)]):
                with self.subTest(argv=argv):
                    self.assertIsNone(checks.noop_reason(argv))
                    self.assertTrue(self.verify(argv).ok)

    def test_verbose_runner_has_a_passing_receipt_for_real_tests(self):
        from factory.core import checks, gate, receipts
        from factory.util import Item

        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder).resolve()
            clone = root / "clone"
            clone.mkdir()
            (root / "item.json").write_text(json.dumps({"schema": 2, "repo": "example/repo", "clone": str(clone),
                "required_checks": [{"label": "regression", "argv": ["./run-tests", "-v"], "cwd": "."}]}))
            item = Item(root)
            runner = clone / "run-tests"
            runner.write_text("#!" + sys.executable + "\n" + textwrap.dedent('''\
                import sys
                import unittest

                sys.dont_write_bytecode = True
                from answer import answer

                class Parser(unittest.TestCase):
                    def test_answer(self):
                        self.assertEqual(answer(), 42)

                unittest.main()
            '''))
            runner.chmod(0o755)
            with patch.object(receipts, "ROOT", root), patch.object(Item, "head_sha", return_value="a" * 40), \
                    patch.object(receipts.clones, "ensure"), \
                    patch.object(receipts, "_state", return_value=("a" * 40, False, 0)), redirect_stdout(io.StringIO()):
                (clone / "answer.py").write_text("def answer():\n    return 41\n")
                red = receipts.run_cmd(item, ["./run-tests", "-v"], expect_fail=True)
                self.assertEqual(red["exit"], 1)
                (clone / "answer.py").write_text("def answer():\n    return 42\n")
                green = receipts.run_cmd(item, ["./run-tests", "-v"])
                self.assertEqual(green["exit"], 0)
                self.assertTrue(receipts.valid(green))
                self.assertTrue(receipts.clean(green))
                self.assertIn("test_answer", "\n".join(green["log_tail"]))
                self.assertIn("Ran 1 test", "\n".join(green["log_tail"]))
                (root / "redgreen.json").write_text(json.dumps({"ok": True, "head_sha": "a" * 40,
                    "cmd": "./run-tests -v", "cwd": ".", "red_exit": red["exit"], "green_exit": green["exit"]}))
                report = gate.Report()
                checks.verify_receipts(item, report)
                self.assertTrue(report.ok, report.text())

    def test_required_inventory_must_include_redgreen_command_and_cwd(self):
        regression = {"cmd": "python -m unittest tests.test_regression", "cwd": "."}
        for argv in (["python", "--version"], ["python", "-c", "print('passed')"], ["python", "-m", "compileall", "."]):
            with self.subTest(argv=argv):
                report = self.verify(argv, redgreen=regression)
                self.assertFalse(report.ok)
                self.assertIn("required_checks must include", report.text())
                self.assertIn("redgreen.json", report.text())
        argv = shlex.split(regression["cmd"])
        self.assertTrue(self.verify(argv, redgreen=regression).ok)
        self.assertTrue(self.verify(["bash", "-lc", regression["cmd"]], redgreen=regression).ok)
        self.assertFalse(self.verify(argv, redgreen={**regression, "cwd": "packages/parser"}).ok)
        self.assertTrue(self.verify(["python", "-m", "compileall", "."], redgreen=regression,
                                    no_test_reason="Documentation-only change; no executable behavior.").ok)
        self.assertFalse(self.verify(["python", "-c", "print('passed')"], redgreen=regression, no_test_reason="").ok)

    def test_invalid_redgreen_cannot_supply_the_designated_test(self):
        for fields in ({"ok": False}, {"head_sha": "b" * 40}, {"cmd": ""}, {"cwd": []}, {"state": "running"}):
            with self.subTest(fields=fields):
                report = self.verify(["python", "-m", "unittest"], redgreen=fields)
                self.assertFalse(report.ok)
                self.assertIn("redgreen", report.text())

    def test_legacy_hook_check_is_not_registered_for_release(self):
        from factory.core import checks

        self.assertEqual(checks.registry()["hook-env"]["schema_min"], 2)


if __name__ == "__main__":
    unittest.main()
