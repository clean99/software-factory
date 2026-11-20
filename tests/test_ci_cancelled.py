import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import setup_module

from factory.oss import followup, pr_signals  # noqa: E402

URL = "https://github.com/o/r/pull/1"

# meta/lexical#9677 statusCheckRollup (gh pr view --json statusCheckRollup, 2026-11-04), trimmed to the duplicated checks.
LEXICAL_9677 = [
    {"__typename": "CheckRun", "completedAt": "2026-10-31T18:38:43Z", "conclusion": "CANCELLED",
     "detailsUrl": "https://github.com/meta/lexical/actions/runs/41230404218/job/124623244893", "name": "changesets",
     "startedAt": "2026-10-31T18:38:37Z", "status": "COMPLETED", "workflowName": "Changesets"},
    {"__typename": "CheckRun", "completedAt": "2026-10-31T18:38:58Z", "conclusion": "SUCCESS",
     "detailsUrl": "https://github.com/meta/lexical/actions/runs/41230412344/job/124623282093", "name": "changesets",
     "startedAt": "2026-10-31T18:38:46Z", "status": "COMPLETED", "workflowName": "Changesets"},
    {"__typename": "CheckRun", "completedAt": "2026-10-31T18:38:47Z", "conclusion": "SKIPPED",
     "detailsUrl": "https://github.com/meta/lexical/actions/runs/41230412349/job/124623298147", "name": "e2e-tests",
     "startedAt": "2026-10-31T18:38:47Z", "status": "COMPLETED", "workflowName": "Lexical Tests (Extended)"},
    {"__typename": "CheckRun", "completedAt": "2026-11-03T07:53:19Z", "conclusion": "SKIPPED",
     "detailsUrl": "https://github.com/meta/lexical/actions/runs/41599275146/job/125793978252", "name": "e2e-tests",
     "startedAt": "2026-11-03T07:53:19Z", "status": "COMPLETED", "workflowName": "Lexical Tests (Extended)"},
    {"__typename": "CheckRun", "completedAt": "2026-10-31T18:38:38Z", "conclusion": "SUCCESS", "detailsUrl": "https://code.facebook.com/cla/",
     "name": "Meta CLA Check", "startedAt": "2026-10-31T18:38:38Z", "status": "COMPLETED", "workflowName": ""},
    {"__typename": "StatusContext", "context": "Vercel – lexical", "startedAt": "2026-10-31T18:40:09Z", "state": "SUCCESS",
     "targetUrl": "https://vercel.com/fbopensource/lexical/DP742f2bthzuRC5UgqxNbu5zw676"},
]


def setUpModule():
    setup_module(unittest)


def check_run(name, conclusion, completed=None, workflow="CI", status="COMPLETED"):
    check = {"__typename": "CheckRun", "name": name, "conclusion": conclusion, "status": status, "workflowName": workflow}
    if completed:
        check.update(startedAt=f"2026-11-01T{completed}:00Z", completedAt=f"2026-11-01T{completed}:30Z")
    return check


def context(name, state, started):
    return {"__typename": "StatusContext", "context": name, "state": state, "startedAt": f"2026-11-01T{started}:00Z"}


class CheckRunHistory(unittest.TestCase):
    def setUp(self):
        self.pr = {"state": "OPEN", "mergedAt": None, "reviewDecision": "", "statusCheckRollup": [], "commits": [],
                   "mergeable": "MERGEABLE", "baseRefName": "main"}
        self.base_runs, self.base_statuses, self.other_prs, self.reads = [], [], [], []
        for mocker in (patch.object(followup, "gh_json", side_effect=self.read),
                       patch.object(followup, "gh_paginate", return_value=[]),
                       patch.object(followup, "repo_mergers", return_value=frozenset()),
                       patch.object(pr_signals, "lines", return_value=[])):
            mocker.start()
            self.addCleanup(mocker.stop)

    def read(self, *args, **kwargs):
        self.reads.append(args[:2])
        if args[:2] == ("pr", "view"):
            return self.pr
        if args[:2] == ("pr", "list"):
            return self.other_prs
        if args[0] == "api" and "/check-runs?" in args[1]:
            return {"check_runs": self.base_runs}
        if args[0] == "api" and "/statuses?" in args[1]:
            return self.base_statuses
        raise AssertionError(f"unexpected read {args}")

    def sweep(self, rollup):
        self.pr["statusCheckRollup"] = rollup
        submitted = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        row = {"url": URL, "repo": "o/r", "status": "submitted", "submitted_at": submitted, "lesson": None}
        actions = []
        followup.check_pr(row, "fixture-owner", actions)
        return actions, row

    def failing(self, rollup):
        actions, row = self.sweep(rollup)
        lines = [a for a in actions if a.startswith("CI FAILING")]
        self.assertEqual(bool(lines), row["status"] == "ci-failing", actions)
        return row["checks"]["failing"], lines

    def test_cancelled_duplicate_superseded_by_a_success_is_not_failing(self):
        failing, lines = self.failing(LEXICAL_9677)
        self.assertEqual(failing, [])
        self.assertEqual(lines, [])

    def test_failure_after_a_success_is_failing(self):
        for rollup in ([check_run("unit", "SUCCESS", "10:00"), check_run("unit", "FAILURE", "10:05")],
                       [check_run("unit", "FAILURE", "10:05"), check_run("unit", "SUCCESS", "10:00")],
                       [check_run("unit", "SUCCESS"), check_run("unit", "FAILURE")]):
            with self.subTest(rollup=rollup):
                failing, lines = self.failing(rollup)
                self.assertEqual(failing, ["unit"])
                self.assertEqual(lines, [f"CI FAILING {URL}: unit — read the logs, fix on the same branch"])
        self.assertEqual(self.failing([check_run("unit", "FAILURE", "10:00"), check_run("unit", "SUCCESS", "10:05")])[0], [])

    def test_cancelled_is_failing_unless_a_later_run_passed(self):
        for rollup in ([check_run("changesets", "CANCELLED", "10:00")],
                       [check_run("changesets", "CANCELLED", "10:00"), check_run("changesets", "CANCELLED", "10:05")],
                       [check_run("changesets", "SUCCESS", "10:00"), check_run("changesets", "CANCELLED", "10:05")],
                       [check_run("changesets", "SUCCESS"), check_run("changesets", "CANCELLED")]):
            with self.subTest(rollup=rollup):
                self.assertEqual(self.failing(rollup)[0], ["changesets"])
        for later in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            with self.subTest(later=later):
                self.assertEqual(self.failing([check_run("changesets", "CANCELLED", "10:00"), check_run("changesets", later, "10:05")])[0], [])

    def test_cancelled_tied_with_a_success_stays_failing(self):
        for rollup in ([check_run("changesets", "CANCELLED", "10:00"), check_run("changesets", "SUCCESS", "10:00")],
                       [check_run("changesets", "SUCCESS", "10:00"), check_run("changesets", "CANCELLED", "10:00")]):
            with self.subTest(rollup=rollup):
                self.assertEqual(self.failing(rollup)[0], ["changesets"])

    def test_status_contexts_are_judged_by_state(self):
        self.assertEqual(self.failing([context("ci/circleci", "FAILURE", "10:00"), context("ci/circleci", "SUCCESS", "10:05")])[0], [])
        self.assertEqual(self.failing([context("ci/circleci", "SUCCESS", "10:00"), context("ci/circleci", "ERROR", "10:05")])[0], ["ci/circleci"])
        self.assertEqual(self.failing([context("ci/circleci", "FAILURE", "10:00"), context("ci/circleci", "PENDING", "10:05")])[0], ["ci/circleci"])
        actions, row = self.sweep([context("Vercel – lexical", "SUCCESS", "10:00"), context("Vercel – lexical", "FAILURE", "10:05")])
        self.assertIn(f"EXTERNAL CHECKS {URL}: Vercel – lexical — deploy/preview bots that need repo-side authorization or config; "
                      "nothing to fix in the PR", actions)
        self.assertFalse(any(a.startswith("CI FAILING") for a in actions), actions)
        self.assertNotEqual(row["status"], "ci-failing")

    def test_same_job_name_in_another_workflow_does_not_hide_a_failure(self):
        rollup = [check_run("build", "FAILURE", "10:00", workflow="CI"), check_run("build", "SUCCESS", "10:05", workflow="Docs")]
        self.assertEqual(self.failing(rollup)[0], ["build"])

    def test_pending_rerun_does_not_clear_a_failure(self):
        rerun = {**check_run("unit", "", status="IN_PROGRESS"), "startedAt": "2026-11-01T10:05:00Z", "completedAt": None}
        failing, _ = self.failing([check_run("unit", "FAILURE", "10:00"), rerun])
        self.assertEqual(failing, ["unit"])

    def test_base_branch_failure_counts_only_when_its_last_run_failed(self):
        self.base_runs = [
            {"name": "changesets", "status": "completed", "conclusion": "cancelled",
             "started_at": "2026-11-01T10:00:00Z", "completed_at": "2026-11-01T10:00:10Z"},
            {"name": "changesets", "status": "completed", "conclusion": "success",
             "started_at": "2026-11-01T10:00:15Z", "completed_at": "2026-11-01T10:01:00Z"},
            {"name": "osv-scan", "status": "completed", "conclusion": "failure",
             "started_at": "2026-11-01T10:00:00Z", "completed_at": "2026-11-01T10:02:00Z"},
        ]
        self.base_statuses = [
            {"context": "lint", "state": "success", "created_at": "2026-11-01T10:05:00Z", "updated_at": "2026-11-01T10:05:00Z"},
            {"context": "lint", "state": "failure", "created_at": "2026-11-01T10:00:00Z", "updated_at": "2026-11-01T10:00:00Z"},
        ]
        self.assertEqual(followup.failing_on_base("o/r", "main", ["changesets", "osv-scan", "lint"], exclude=1), {"osv-scan"})

    def test_other_prs_count_once_each_by_their_last_run(self):
        def prs(*rollups):
            return [{"number": 1, "statusCheckRollup": [check_run("e2e", "FAILURE", "10:00")]}] + [
                {"number": n, "statusCheckRollup": rollup} for n, rollup in enumerate(rollups, start=10)]
        repeated = [check_run("e2e", "FAILURE", "10:00"), check_run("e2e", "FAILURE", "10:05"), check_run("e2e", "FAILURE", "10:10")]
        superseded = [check_run("e2e", "CANCELLED", "10:00"), check_run("e2e", "SUCCESS", "10:05")]
        failed = [check_run("e2e", "CANCELLED", "10:00"), check_run("e2e", "FAILURE", "10:05")]
        for others, expected in ((prs(repeated), set()),
                                 (prs(superseded, superseded, superseded), set()),
                                 (prs(failed, failed, superseded), set()),
                                 (prs(failed, failed, failed), {"e2e"})):
            with self.subTest(others=others):
                self.other_prs = others
                self.assertEqual(followup.failing_on_other_prs("o/r", ["e2e"], exclude=1), expected)


if __name__ == "__main__":
    unittest.main()
