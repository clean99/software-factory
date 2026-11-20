import io
import json
import os
import pathlib
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import patch

import run_suite


class SuiteRunner(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.tests = self.root / "tests"
        self.tests.mkdir()
        self.path = list(sys.path)
        self.addCleanup(setattr, sys, "path", self.path)
        self.modules = set(sys.modules)
        self.addCleanup(self.clear_modules)

    def clear_modules(self):
        for name in set(sys.modules) - self.modules:
            if name.startswith("test_runner_fixture"):
                del sys.modules[name]

    def module(self, name, body):
        (self.tests / f"{name}.py").write_text(textwrap.dedent(body))

    def test_discovery_keeps_nested_tests_and_module_fixtures(self):
        package = self.tests / "test_runner_fixture_package"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "test_nested.py").write_text(textwrap.dedent('''
            import unittest
            ready = False
            def setUpModule():
                global ready
                ready = True
            class Cases(unittest.TestCase):
                def test_ready(self):
                    self.assertTrue(ready)
        '''))
        groups = run_suite.discover(self.tests)
        ids = ["test_runner_fixture_package.test_nested.Cases.test_ready"]
        self.assertEqual(list(groups.values()), [ids])
        records = run_suite.run(groups, self.root, 2, io.StringIO())
        self.assertEqual(records[0]["test_ids"], ids)
        self.assertEqual(records[0]["exit_code"], 0)

    def test_processes_isolate_modules_and_keep_failures_visible(self):
        self.module("test_runner_fixture_first", '''
            import os, unittest
            class Cases(unittest.TestCase):
                def test_mutate(self):
                    os.environ["FACTORY_TEST_CROSSTALK"] = "changed"
        ''')
        self.module("test_runner_fixture_second", '''
            import os, unittest
            class Cases(unittest.TestCase):
                def test_isolated(self):
                    self.assertNotIn("FACTORY_TEST_CROSSTALK", os.environ)
                def test_failure(self):
                    self.fail("fixture failure stays visible")
        ''')
        groups = run_suite.discover(self.tests)
        output = io.StringIO()
        records = run_suite.run(groups, self.root, 1, output)
        self.assertEqual([row["exit_code"] for row in records], [0, 1])
        self.assertEqual(sum(row["tests"] for row in records), 3)
        self.assertIn("fixture failure stays visible", output.getvalue())
        self.assertIn("Ran 2 tests", output.getvalue())
        self.assertNotIn("FACTORY_TEST_CROSSTALK", os.environ)
        self.assertTrue(all(row["seconds"] > 0 for row in records))

    def test_workers_keep_load_tests_case_state(self):
        self.module("test_runner_fixture_loaded", '''
            import unittest
            class Cases(unittest.TestCase):
                value = "default"
                def test_loaded(self):
                    self.assertEqual(self.value, "default")
            def load_tests(loader, tests, pattern):
                case = Cases("test_loaded")
                case.value = "loaded-failure"
                return unittest.TestSuite([case])
        ''')
        output = io.StringIO()
        records = run_suite.run(run_suite.discover(self.tests), self.root, 2, output)
        self.assertEqual(records[0]["exit_code"], 1)
        self.assertIn("'loaded-failure' != 'default'", output.getvalue())
        self.assertIn("Ran 1 test", output.getvalue())

    def test_workers_keep_module_import_skips(self):
        self.module("test_runner_fixture_skip", '''
            import unittest
            raise unittest.SkipTest("optional dependency unavailable")
        ''')
        output = io.StringIO()
        records = run_suite.run(run_suite.discover(self.tests), self.root, 2, output)
        self.assertEqual(records[0]["exit_code"], 0)
        self.assertEqual(records[0]["tests"], 1)
        self.assertIn("OK (skipped=1)", output.getvalue())

    def test_workers_keep_generated_cases_and_custom_suites(self):
        self.module("test_runner_fixture_custom", '''
            import unittest
            def load_tests(loader, tests, pattern):
                class Generated(unittest.TestCase):
                    ready = False
                    def runTest(self):
                        self.assertTrue(self.ready)
                class PreparedSuite(unittest.TestSuite):
                    def run(self, result, debug=False):
                        for case in self:
                            case.ready = True
                        return super().run(result, debug)
                return PreparedSuite([Generated()])
        ''')
        output = io.StringIO()
        records = run_suite.run(run_suite.discover(self.tests), self.root, 2, output)
        self.assertEqual(records[0]["exit_code"], 0, output.getvalue())
        self.assertIn("Ran 1 test", output.getvalue())

    def test_worker_rejects_a_changed_discovery_set(self):
        self.module("test_runner_fixture_changed", '''
            import unittest
            class Cases(unittest.TestCase):
                def test_before(self):
                    pass
        ''')
        groups = run_suite.discover(self.tests)
        self.module("test_runner_fixture_changed", '''
            import unittest
            class Cases(unittest.TestCase):
                def test_after(self):
                    pass
        ''')
        output = io.StringIO()
        records = run_suite.run(groups, self.root, 1, output)
        self.assertEqual(records[0]["exit_code"], 1)
        self.assertIn("Worker discovery differs", output.getvalue())
        for index in (-1, 100):
            with self.assertRaisesRegex(ValueError, "Worker discovery differs"):
                run_suite.run_worker(index, self.tests, [])

    def test_jobs_bound_parallel_execution(self):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active, maximum = 0, 0

        def module(name, ids, root):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            barrier.wait(timeout=10)
            with lock:
                active -= 1
            return {"module": name, "tests": 1, "seconds": 0, "exit_code": 0}, "ok\n"

        groups = {str(i): [str(i)] for i in range(4)}
        with patch.object(run_suite, "run_module", side_effect=module):
            records = run_suite.run(groups, self.root, 2, io.StringIO())
        self.assertEqual(maximum, 2)
        self.assertEqual([row["module"] for row in records], list(groups))

    def test_empty_suite_and_import_errors_fail(self):
        with self.assertRaisesRegex(ValueError, "No tests"):
            run_suite.discover(self.tests)
        self.module("test_runner_fixture_broken", 'raise RuntimeError("broken import")')
        with self.assertRaisesRegex(ValueError, "broken import"):
            run_suite.discover(self.tests)

    def test_main_writes_timings_and_propagates_nonzero_exit(self):
        report = self.root / "results.json"
        self.module("test_runner_fixture_status", '''
            import unittest
            class Cases(unittest.TestCase):
                def test_failure(self):
                    self.fail("intentional failure")
        ''')
        with patch.object(run_suite, "ROOT", self.root), patch.object(sys, "stdout", io.StringIO()) as output:
            code = run_suite.main(["--jobs", "2", "--json", str(report)])
        self.assertEqual(code, 1)
        data = json.loads(report.read_text())
        self.assertFalse(data["success"])
        self.assertEqual((data["tests"], data["jobs"]), (1, 2))
        self.assertGreater(data["wall_seconds"], 0)
        self.assertIn("FAILED: 1 tests", output.getvalue())

    def test_main_rejects_nonpositive_jobs_before_discovery(self):
        for jobs in ("0", "-1"):
            with patch.object(run_suite, "discover") as discover, patch.object(sys, "stderr", io.StringIO()), \
                    self.assertRaises(SystemExit) as status:
                run_suite.main(["--jobs", jobs])
            self.assertEqual(status.exception.code, 2)
            discover.assert_not_called()
