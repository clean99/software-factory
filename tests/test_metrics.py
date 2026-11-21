import datetime as dt
import io
import json
import pathlib
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlsplit
from contextlib import redirect_stdout
from unittest.mock import patch

from support import setup_module
from factory import cli
from factory.core import leaks, metrics, metrics_site, privacy
from factory.util import CommandError, load_config


def setUpModule():
    setup_module(unittest)


CONFIG = {"github_user": "example", "public": {"repository": "example/factory", "dashboard_url": "https://example.github.io/factory/"},
          "repos_dir": "workspace/repos", "runs_dir": "runs", "ledger_file": "ledger/prs.json"}
NOW = dt.datetime(2026, 1, 25, tzinfo=dt.timezone.utc)


def pr(number, status="merged", submitted="2026-01-05T00:00:00Z", merged="2026-01-06T00:00:00Z", **fields):
    return {"url": f"https://github.com/example/project/pull/{number}", "status": status,
            "submitted_at": submitted, "merged_at": merged if status == "merged" else None, **fields}


def item(number, **fields):
    return {"key": f"2026-01-04/candidate-{number}",
            "meta": {"created_at": "2026-01-04T00:00:00Z", "pr_url": pr(number)["url"]},
            "qualify": {"verdict": "ok"}, "value": {"verdict": "build", "complete": True},
            "reviews": [{"verdict": "ship", "head": "a" * 40}], "receipts": [], **fields}


def snapshot():
    return {"prs": [pr(1, responded={"maintainer": True}, checks={"failing": [], "total": 2}),
                    pr(2, merged="2026-01-08T00:00:00Z", responded={"maintainer": False}, checks={"failing": ["unit"], "total": 3}),
                    pr(3, "closed", responded={"maintainer": False}), pr(4, "submitted")],
            "items": [item(1, reviews=[{"verdict": "fix-needed", "head": "b" * 40}, {"verdict": "ship", "head": "a" * 40}]),
                      item(2), item(3, qualify={"verdict": "skip"}, value={"verdict": "drop", "complete": True}),
                      item(4, value=None, reviews=[], qualify=None)],
            "shifts": [{"status": "exited 0", "active_minutes": 10, "tool_calls": 20},
                       {"status": "exited 1", "active_minutes": 5, "tool_calls": 10}, {"status": "exited 0"}],
            "events": [{"url": pr(2)["url"], "kind": "ci-failing", "at": "2026-01-05T08:00:00Z"}] * 2,
            "suite": {"success": True, "tests": 4, "wall_seconds": 2.5,
                      "modules": [{"tests": 4, "seconds": 2, "exit_code": 0}]},
            "history": {"commits": 8, "fix_commits": 3}, "owner_ops": [{"action": "close-pr"}], "owner_action_rows": 2}


class MetricsTests(unittest.TestCase):
    def report(self, data=None):
        return metrics.compute(snapshot() if data is None else data, CONFIG, NOW)

    def test_every_contract_metric_has_a_definition_source_coverage_and_status(self):
        rows = self.report()["metrics"]
        expected = {
            "prs_opened": 4, "prs_merged": 2, "prs_open": 1, "prs_closed": 1, "decided_merge_rate": .667,
            "candidates_stopped_before_publish": 0, "median_lead_hours": 24,
            "ci_failures_after_submission": 1, "independent_review_rounds_per_pr": 1.333,
            "candidates_found": 4, "dropped_qualify": 1, "dropped_value": 1, "sent_back_review": 1,
            "coordinator_minutes": 15, "coordinator_tool_calls": 30, "shift_completion_rate": .667,
            "factory_test_count": 4, "factory_test_runtime": 2.5, "factory_fix_commits": 3,
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(rows[key]["value"], value)
        untracked = {"cohort_merge_rate", "merged_no_rework_share", "review_fix_hours", "fixes_pushed_after_review", "candidate_selection_misses",
                     "fixes_pushed_before_review",
                     "post_merge_reverts", "post_merge_regressions", "agent_minutes_per_pr", "tool_calls_per_pr",
                     "agent_minutes_per_merge", "tool_calls_per_merge", "unattended_shifts", "owner_interventions",
                     "factory_defects_found", "factory_defects_fixed"}
        self.assertEqual(set(rows), set(expected) | untracked)
        for key, row in rows.items():
            self.assertTrue(row["definition"] and row["source"], key)
            self.assertGreaterEqual(row["coverage"]["total"], row["coverage"]["covered"], key)
            if key in untracked:
                self.assertEqual(row["status"], "not_tracked", key)
                self.assertIsNone(row["value"], key)
                self.assertTrue(row["reason"], key)

    def test_missing_legacy_fields_do_not_mean_zero_or_open(self):
        rows = self.report({"prs": [pr(1, "abandoned"), {"url": pr(2)["url"]}, None, {}]})["metrics"]
        self.assertEqual(rows["prs_opened"]["value"], 2)
        self.assertIsNone(rows["prs_open"]["value"])
        self.assertEqual(rows["prs_open"]["coverage"], {"covered": 0, "total": 2})
        for key in ("decided_merge_rate", "cohort_merge_rate", "merged_no_rework_share", "review_fix_hours", "median_lead_hours",
                    "ci_failures_after_submission", "independent_review_rounds_per_pr", "dropped_qualify", "dropped_value",
                    "sent_back_review", "coordinator_minutes", "coordinator_tool_calls", "shift_completion_rate",
                    "factory_test_count", "factory_test_runtime", "factory_fix_commits"):
            self.assertIsNone(rows[key]["value"], key)
        self.assertEqual(rows["prs_opened"]["status"], "partial")

    def test_duplicate_urls_keep_latest_snapshot_and_do_not_inflate_counts(self):
        data = {"prs": [pr(1, "merged", last_check="2026-01-07T00:00:00Z"),
                        {**pr(1, "submitted"), "url": pr(1)["url"].replace("example/project", "EXAMPLE/PROJECT") + "/",
                         "last_check": "2026-01-06T00:00:00Z"}]}
        result = self.report(data)
        self.assertEqual(result["metrics"]["prs_opened"]["value"], 1)
        self.assertEqual(result["metrics"]["prs_merged"]["value"], 1)
        self.assertEqual(result["input_quality"]["duplicate_pr_rows"], 1)

    def test_superseded_waiting_time_and_silence_metrics_are_not_exported(self):
        result = self.report()
        excluded = {"no_maintainer_response_share", "first_response_hours", "median_merge_hours", "p90_merge_hours",
                    "merged_needing_fix_share", "response_samples", "no_response", "merge_time_samples",
                    "first_round_approval_share", "review_rounds_per_pr", "closed_unmerged_share", "merge_rate"}
        self.assertTrue(excluded.isdisjoint(result["metrics"]))
        for row in result["weekly"]["rows"]:
            self.assertTrue(excluded.isdisjoint(row))

    def test_durations_exclude_dates_naive_reversed_and_invalid_timestamps(self):
        data = {"prs": [pr(1), pr(2, submitted="2026-01-03T00:00:00Z"), pr(3, submitted="2026-01-08"),
                        pr(4, submitted="2026-01-08T00:00:00"), pr(5, submitted="broken"), pr(6, submitted=None)],
                "items": [item(n) for n in range(1, 7)]}
        row = self.report(data)["metrics"]["median_lead_hours"]
        self.assertEqual(row["value"], 24)
        self.assertEqual(row["coverage"], {"covered": 1, "total": 6})
        self.assertEqual(metrics.elapsed("2026-01-01T08:00:00+08:00", "2026-01-02T00:00:00Z"), 24)
        self.assertIsNone(metrics.timestamp(42))

    def test_lead_time_can_match_a_legacy_item_reference_without_opening_it(self):
        data = snapshot()
        data["items"][0]["meta"].pop("pr_url")
        data["prs"][0]["item"] = "/old-checkout/runs/2026-01-04/candidate-1"
        self.assertEqual(self.report(data)["metrics"]["median_lead_hours"]["coverage"]["covered"], 4)

    def test_weekly_event_output_and_submission_cohort_acceptance_are_distinct(self):
        data = {"prs": [pr(1, submitted="2026-01-04T23:00:00Z", merged="2026-01-19T00:00:00Z"),
                        pr(2, "closed", submitted="2026-01-04T23:00:00Z"),
                        pr(3, "open", submitted="2026-01-12T00:00:00Z")],
                "items": [item(1), item(2), item(3)]}
        result = self.report(data)["weekly"]
        rows = result["rows"]
        self.assertEqual([r["week"] for r in rows], ["2025-12-29", "2026-01-05", "2026-01-12", "2026-01-19"])
        self.assertEqual(rows[0]["opened"], 2)
        self.assertEqual(rows[0]["merged"], 0)
        self.assertEqual(rows[0]["cohort_merged"], 1)
        self.assertEqual(rows[0]["cohort_closed"], 1)
        self.assertEqual(rows[3]["merged"], 1)
        self.assertEqual(rows[2]["cohort_open"], 1)
        self.assertEqual(rows[1]["cohort_merged"], 0)
        self.assertIsNone(rows[1]["median_lead_hours"])
        self.assertEqual(rows[0]["median_lead_hours"], 23)
        self.assertEqual(rows[0]["lead_time_samples"], 2)
        self.assertEqual(rows[2]["median_lead_hours"], 192)
        self.assertIsNone(rows[3]["median_lead_hours"])
        self.assertIsNone(rows[3]["review_fix_hours"])
        self.assertEqual(rows[3]["cohort_no_rework"], 0)
        self.assertIsNone(rows[3]["owner_interventions"])

    def test_missing_dates_do_not_appear_in_weekly_series(self):
        data = {"prs": [pr(1, submitted=None), pr(2, merged=None)]}
        series = self.report(data)["weekly"]
        self.assertEqual(series["coverage"]["submission_dates"], {"covered": 1, "total": 2})
        self.assertEqual(series["coverage"]["merge_dates"], {"covered": 1, "total": 2})
        self.assertTrue(all(r["median_lead_hours"] is None for r in series["rows"]))

    def test_partial_events_and_latest_push_do_not_prove_first_round_quality_or_a_fix(self):
        data = snapshot()
        data["events"].append({"kind": "maintainer-review", "at": "2026-01-05T00:00:00Z", "url": pr(1)["url"],
                               "detail": {"state": "CHANGES_REQUESTED"}})
        data["events"].append({"kind": "maintainer-review", "at": "2026-01-06T00:00:00Z", "url": pr(2)["url"],
                               "detail": {"state": "APPROVED"}})
        data["items"][0]["meta"].update(pushed_at="2026-01-05T06:00:00Z", pushed_head="c" * 40)
        data["prs"][2]["notes"] = "duplicate, already fixed, unwanted"
        rows = self.report(data)["metrics"]
        for key in ("merged_no_rework_share", "fixes_pushed_after_review", "review_fix_hours", "candidate_selection_misses",
                    "owner_interventions", "unattended_shifts", "agent_minutes_per_pr", "tool_calls_per_pr"):
            self.assertEqual(rows[key]["status"], "not_tracked", key)
            self.assertEqual(rows[key]["coverage"]["covered"], 0, key)
            self.assertTrue(rows[key]["reason"], key)
        self.assertEqual(rows["merged_no_rework_share"]["coverage"]["total"], 2)
        self.assertEqual(rows["candidate_selection_misses"]["coverage"]["total"], 1)

    def test_ci_counts_distinct_prs_and_does_not_treat_empty_checks_as_observation(self):
        data = snapshot()
        data["prs"][0]["checks"] = {"failing": [], "total": 0}
        data["events"].append({"kind": "ci-failing", "url": pr(999)["url"]})
        row = self.report(data)["metrics"]["ci_failures_after_submission"]
        self.assertEqual(row["value"], 1)
        self.assertEqual(row["coverage"], {"covered": 1, "total": 4})
        self.assertEqual(row["status"], "partial")

    def test_invalid_numbers_and_incomplete_reviews_do_not_count(self):
        data = snapshot()
        data["shifts"] += [{"active_minutes": True, "tool_calls": float("nan")}, {"active_minutes": -1, "tool_calls": "200"}]
        data["items"][2]["value"]["complete"] = False
        rows = self.report(data)["metrics"]
        self.assertEqual(rows["coordinator_minutes"]["value"], 15)
        self.assertEqual(rows["coordinator_tool_calls"]["value"], 30)
        self.assertEqual(rows["dropped_value"]["value"], 0)
        self.assertEqual(rows["dropped_value"]["coverage"], {"covered": 2, "total": 4})

    def test_suite_report_is_validated_and_failed_suites_are_labeled(self):
        data = snapshot()
        data["suite"]["tests"] = 5
        self.assertIsNone(self.report(data)["metrics"]["factory_test_count"]["value"])
        data["suite"]["tests"] = 4
        data["suite"]["success"] = False
        data["suite"]["modules"][0]["exit_code"] = 1
        row = self.report(data)["metrics"]["factory_test_count"]
        self.assertEqual(row["value"], 4)
        self.assertIn("FAILED", row["note"])

    def test_session_since_uses_the_first_valid_utc_summary_date(self):
        data = snapshot()
        data['shifts'] = [{'status': 'exited 0', 'ended_at': '2026-01-24T02:00:00+08:00'},
                          {'status': 'exited 1', 'ended_at': '2026-01-25T00:00:00Z'},
                          {'status': 'exited 0', 'ended_at': '2026-01-01'}]
        result = self.report(data)
        self.assertEqual(result['metrics']['shift_completion_rate']['since'], '2026-01-23')
        self.assertEqual(metrics_site.fact_values(result['metrics'])['SHIFTS_DETAIL'],
                         'session summaries kept since 2026-01-23; a clean exit is not a finished queue')
        self.assertEqual(result['metrics']['decided_merge_rate']['note'],
                         'Leaves out pull requests that are still open, so on the same data it reads higher than the cohort merge rate.')

    def test_entire_output_passes_existing_privacy_and_leak_scanners(self):
        data = snapshot()
        private = "private-workstation /home/user notes secret-person@example.test " + "ghp_" + "x" * 36
        for row in data["prs"]:
            row.update(notes=private, account_id="abcdef" * 6, maintainer="private-maintainer")
        for row in data["items"]:
            row["meta"].update(clone=private, notes=private)
            row["receipts"] = [{"cwd": private, "log_tail": [private], "duration_s": 12}]
        data["shifts"][0]["final"] = private
        data["events"][0]["detail"] = {"body": private, "author_login": "private-maintainer"}
        data["owner_ops"][0]["note"] = private
        result = self.report(data)
        for output in (json.dumps(result), metrics_site.render(result), metrics_site.render_board(result)):
            self.assertNotIn("private-fixture", output)
            self.assertNotIn("private-maintainer", output)
            self.assertNotIn("account_id", output)
            self.assertNotIn("secret-person", output)
            self.assertEqual(privacy.findings("output", output, prose=True, markers=["private-workstation"]), [])
            self.assertEqual(leaks.scan(text=output, label="output"), [])

    def test_links_are_only_configured_public_surfaces(self):
        result = self.report()
        self.assertEqual(result["links"]["repository"], "https://github.com/example/factory")
        for overrides in ({"repository": "../../private"},
                          {"dashboard_url": "https://name:password@example.test/"},
                          {"dashboard_url": "https://example.test/?token=secret"}):
            with self.assertRaises(CommandError):
                metrics.public_links({**CONFIG, "public": {**CONFIG["public"], **overrides}})


class CohortTests(unittest.TestCase):
    def report(self, rows, **fields):
        return metrics.compute({"prs": rows, **fields}, CONFIG, NOW)

    def histories(self, rows):
        return {"schema_version": 1, "prs": {r["url"]: {
            "status": "available", "state": r["status"], "created_at": r["submitted_at"],
            "merged_at": r["merged_at"], "reviews": [], "commits": [], "events": []} for r in rows}}

    def test_cutoff_includes_the_whole_utc_date_and_cached_creation_takes_precedence(self):
        rows = [pr(1, submitted="2026-01-11T00:00:00Z"), pr(2, submitted="2026-01-11T23:59:59Z"),
                pr(3, submitted="2026-01-12T00:00:00Z"), pr(4, submitted="2026-01-11"),
                pr(5, submitted=None), pr(6, submitted="2026-01-20T00:00:00Z")]
        saved = self.histories(rows)
        saved["prs"][pr(6)["url"]]["created_at"] = "2026-01-04T00:00:00Z"
        result = self.report(rows, pr_history=saved)
        cohort = result["cohort"]
        self.assertEqual((cohort["n"], cohort["excluded_recent"], cohort["excluded_undated"]), (3, 1, 2))
        self.assertEqual((cohort["maturity_days"], cohort["min_sample"], cohort["as_of"]), (14, 20, "2026-01-25"))
        self.assertEqual(cohort["cutoff"], "2026-01-11")
        self.assertEqual((cohort["first_opened"], cohort["last_opened"]), ("2026-01-04", "2026-01-11"))
        self.assertEqual([r["url"] for r in result["pull_requests"] if r["in_cohort"]], [pr(6)["url"], pr(1)["url"], pr(2)["url"]])
        self.assertEqual(sum(r["opened"] is None for r in result["pull_requests"]), 2)
        later = metrics.compute({"prs": rows, "pr_history": saved}, CONFIG, NOW.replace(hour=23, minute=59, second=59))
        self.assertEqual(later["cohort"], cohort)

    def test_cutoff_uses_utc_dates_for_opening_and_snapshot_timestamps(self):
        rows = [pr(1, submitted="2026-01-12T07:59:59+08:00"), pr(2, submitted="2026-01-11T20:00:00-04:00")]
        snapshot_time = dt.datetime(2026, 1, 26, 7, 59, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        result = metrics.compute({"prs": rows}, CONFIG, snapshot_time)
        self.assertEqual((result["cohort"]["as_of"], result["cohort"]["cutoff"], result["cohort"]["n"]),
                         ("2026-01-25", "2026-01-11", 1))
        self.assertEqual([r["in_cohort"] for r in result["pull_requests"]], [True, False])

    def test_minimum_sample_and_incomplete_history_coverage(self):
        rows = [pr(n) for n in range(1, 21)]
        for n, shown in ((19, False), (20, True)):
            report = self.report(rows[:n], pr_history=self.histories(rows[:n]))
            for key in ("cohort_merge_rate", "merged_no_rework_share"):
                row = report["metrics"][key]
                self.assertEqual((row["numerator"], row["denominator"]), (n, n))
                self.assertEqual(row["value"], 1 if shown else None)
                self.assertEqual(bool(row["reason"]), not shown)
        saved = self.histories(rows)
        saved["prs"].pop(pr(20)["url"])
        report = self.report(rows, pr_history=saved)
        self.assertEqual(report["cohort"]["merged_unverified"], 1)
        self.assertEqual(report["metrics"]["cohort_merge_rate"]["value"], 1)
        share = report["metrics"]["merged_no_rework_share"]
        self.assertEqual((share["numerator"], share["denominator"]), (19, 19))
        self.assertEqual(share["coverage"], {"covered": 19, "total": 20})
        self.assertIsNone(share["value"])

    def test_closed_and_unanswered_stay_in_denominator_and_later_merge_moves_row(self):
        rows = [pr(n) for n in range(1, 19)] + [pr(19, "closed"), pr(20, "silent")]
        result = self.report(rows)
        rate = result["metrics"]["cohort_merge_rate"]
        self.assertEqual((rate["numerator"], rate["denominator"], rate["open"], rate["closed"]), (18, 20, 1, 1))
        self.assertEqual(rate["value"], .9)
        self.assertEqual(result["reasons"]["open"], [{"key": "no-reply", "n": 1, "pull_requests": [pr(20)["url"]]}])
        rows[-1] = pr(20)
        updated = self.report(rows)
        self.assertEqual((updated["cohort"]["n"], updated["cohort"]["merged"], updated["cohort"]["open"]), (20, 19, 0))
        self.assertEqual(updated["metrics"]["cohort_merge_rate"]["value"], .95)

    def test_wilson_interval_known_values(self):
        self.assertEqual(metrics.wilson(50, 100), [.4038, .5962])
        self.assertEqual(metrics.wilson(0, 20), [0, .1611])
        self.assertIsNone(metrics.wilson(0, 0))
        result = self.report([pr(n) for n in range(1, 21)])
        self.assertEqual(result["metrics"]["cohort_merge_rate"]["interval"], [.8389, 1])

    def test_repository_pooling_and_reason_order_are_cohort_only(self):
        rows = [pr(n, "closed") for n in range(1, 6)] + [
            {**pr(6, "silent"), "url": "https://github.com/example/second/pull/6"},
            {**pr(7), "url": "https://github.com/example/third/pull/7"},
            pr(8, "closed", submitted="2026-01-24T00:00:00Z")]
        entries = {pr(n)["url"]: {"reason": reason, "note": "", "evidence": pr(n)["url"]}
                   for n, reason in ((1, "withdrawn"), (2, "duplicate"), (3, "withdrawn"), (4, "duplicate"))}
        result = self.report(rows, outcomes={"schema": 1, "closures": entries})
        self.assertEqual(result["reasons"]["closed"], [
            {"key": "duplicate", "n": 2, "pull_requests": [pr(2)["url"], pr(4)["url"]]},
            {"key": "withdrawn", "n": 2, "pull_requests": [pr(1)["url"], pr(3)["url"]]},
            {"key": "unclassified", "n": 1, "pull_requests": [pr(5)["url"]]}])
        self.assertEqual(result["by_repository"], [
            {"repository": "example/project", "opened": 5, "merged": 0, "merged_no_rework": 0, "closed": 5, "open": 0},
            {"repository": "other", "repositories": 2, "opened": 2, "merged": 1, "merged_no_rework": 0, "closed": 0, "open": 1}])

    def test_outcomes_renames_validation_and_no_private_fields_escape(self):
        old = "https://github.com/Facebook/Lexical/pull/1/"
        current = "https://github.com/meta/lexical/pull/1"
        entry = {"reason": "duplicate", "evidence": current + "#issuecomment-1", "note": "private-person@example.test"}
        invalid = [{**entry, "reason": "bad"}, {**entry, "evidence": "https://example.test/"},
                   {**entry, "evidence": "https://" + "name:password@" + "github.com/example/project/pull/2"},
                   {**entry, "evidence": "https://github.com.evil.test/"}, {**entry, "note": 2}, {**entry, "note": "x" * 201}]
        closures = {old: entry, **{pr(n)["url"]: row for n, row in enumerate(invalid, 2)}}
        with patch.dict(metrics.CONFIG, {"repo_renames": {"facebook/lexical": "meta/lexical"}}):
            result = self.report([{**pr(1, "closed"), "url": current}, {**pr(1, "closed"), "url": old}],
                                 outcomes={"schema": 1, "closures": closures})
        self.assertEqual(result["input_quality"]["invalid_outcome_rows"], len(invalid))
        self.assertEqual(result["input_quality"]["duplicate_pr_rows"], 1)
        self.assertEqual(result["pull_requests"], [{"url": current, "repository": "meta/lexical", "number": 1,
                         "opened": "2026-01-05", "in_cohort": True, "bucket": "closed", "reason": "duplicate",
                         "flags": [], "evidence": entry["evidence"], "edit_class": None}])
        self.assertNotIn("private-person", json.dumps(result))
        self.assertEqual(result["reasons"]["closed"][-1], {"key": "unclassified", "n": 0, "pull_requests": []})

    def test_repository_renames_pool_rows_and_histories_under_one_public_name(self):
        rows = [{**pr(n), "repo": "facebook/lexical",
                 "url": f"https://github.com/{'facebook/lexical' if n % 2 else 'meta/lexical'}/pull/{n}"}
                for n in range(1, 6)]
        rows.append({**rows[0], "url": "https://github.com/meta/lexical/pull/1"})
        with patch.dict(metrics.CONFIG, {"repo_renames": {"facebook/lexical": "meta/lexical"}}):
            result = self.report(rows, pr_history=self.histories(rows))
        self.assertEqual(result["input_quality"]["duplicate_pr_rows"], 1)
        self.assertEqual(result["cohort"]["n"], 5)
        self.assertEqual(result["cohort"]["merged_no_rework"], 5)
        self.assertEqual({r["repository"] for r in result["pull_requests"]}, {"meta/lexical"})
        self.assertTrue(all(r["url"].startswith("https://github.com/meta/lexical/pull/") for r in result["pull_requests"]))
        self.assertEqual(result["by_repository"], [{"repository": "meta/lexical", "opened": 5, "merged": 5,
                                                 "merged_no_rework": 5, "closed": 0, "open": 0}])

    def test_repository_spelling_comes_from_the_ledger_without_splitting_case_variants(self):
        rows = [{**pr(n), "repo": "Example/Project" if n % 2 else "example/project"} for n in range(1, 6)]
        rows += [{**pr(6, "closed"), "url": "https://github.com/Another/Project/pull/6"},
                 {**pr(7, submitted="2026-01-24T00:00:00Z"), "repo": "Example/Recent",
                  "url": "https://github.com/example/recent/pull/7"}]
        result = self.report(rows)
        self.assertEqual(result["by_repository"][0]["repository"], "Example/Project")
        self.assertEqual(result["by_repository"][0]["opened"], 5)
        self.assertEqual({r["repository"] for r in result["pull_requests"] if '/example/project/' in r["url"]}, {"Example/Project"})
        self.assertEqual(next(r["repository"] for r in result["pull_requests"] if r["number"] == 6), "another/project")
        self.assertEqual((result["population"]["projects"], result["population"]["merged_projects"]), (3, 2))
        for name in ("unrelated/project", "<script>/project", None):
            with self.subTest(name=name):
                report = self.report([pr(1, repo=name)])
                self.assertEqual(report["pull_requests"][0]["repository"], "example/project")

    def test_repository_display_case_does_not_change_tie_order(self):
        rows = [{**pr(n), "repo": repo, "url": f"https://github.com/{repo}/pull/{n}"}
                for repo in ("Example/Zebra", "example/alpha") for n in range(1, 6)]
        result = self.report(rows)
        self.assertEqual([r["repository"] for r in result["by_repository"]], ["example/alpha", "Example/Zebra"])

    def test_unclassified_edits_count_even_when_another_reason_takes_precedence(self):
        from tests.test_metrics_history import record, review, commit

        rows = [pr(1)]
        saved = self.histories(rows)
        saved["prs"][pr(1)["url"]] = record(reviews=[review("CHANGES_REQUESTED")], commits=[commit(role="other")])
        result = self.report(rows, pr_history=saved)
        self.assertEqual(result["metrics"]["merged_no_rework_share"]["unclassified_edits"], 1)
        self.assertEqual(result["reasons"]["merged_reworked"], [{"key": "change-requested", "n": 1, "pull_requests": [pr(1)["url"]]}])

    def test_neutral_edits_are_counted_even_with_author_rework_but_only_in_cohort(self):
        from tests.test_metrics_history import record, commit

        rows = [pr(n) for n in range(1, 5)]
        rows[-1]["submitted_at"] = "2026-01-24T00:00:00Z"
        saved = self.histories(rows)
        edits = {}
        for n, kind in enumerate(("scope", "refactor", "repair", "tests"), 1):
            row = record(commits=[commit(role="other")] + ([commit(2)] if n == 2 else []))
            row["created_at"] = rows[n - 1]["submitted_at"]
            if n == 4:
                row["commits"][0]["committed_at"] = "2026-01-24T01:00:00Z"
                row["merged_at"] = "2026-01-24T02:00:00Z"
            saved["prs"][pr(n)["url"]] = row
            edits[pr(n)["url"]] = {"class": kind, "evidence": pr(n)["url"], "note": "private edit note"}
        result = self.report(rows, pr_history=saved, outcomes={"schema": 1, "maintainer_edits": edits})
        self.assertEqual(result["cohort"]["maintainer_edits_neutral"], 2)
        self.assertEqual(result["cohort"]["merged_no_rework"], 1)
        self.assertEqual(result["cohort"]["merged_reworked"], 2)
        self.assertEqual([r["edit_class"] for r in result["pull_requests"]], ["scope", "refactor", "repair", "tests"])
        self.assertEqual([r["reason"] for r in result["pull_requests"]], [None, "author-update", "maintainer-repair", None])
        self.assertNotIn("private edit note", json.dumps(result))

    def test_filtering_counts_only_stopped_items_without_a_pull_request(self):
        items = [item(n, meta={"status": status}) for n, status in enumerate(("dropped", "parked", "blocked", "abandoned", "ready"))]
        items += [item(8, meta={"status": "dropped", "pr_url": pr(8)["url"]}),
                  item(9, meta={"status": "blocked"})]
        result = self.report([pr(9, item="runs/2026-01-04/candidate-9")], items=items)
        row = result["metrics"]["candidates_stopped_before_publish"]
        self.assertEqual(row["value"], 4)
        for status in ("dropped", "parked", "blocked", "abandoned"):
            self.assertIn(status, row["definition"])

    def test_reconciliation_is_counts_only_or_not_checked(self):
        self.assertEqual(self.report([])["reconciliation"], {"status": "not_checked"})
        check = {"checked_at": "2026-01-25T00:00:00Z", "github": 21, "ledger": 20, "github_only": 2, "ledger_only": 1}
        result = self.report([], pr_history={"schema_version": 1, "prs": {}, "reconciliation": {**check, "login": "private-user"}})
        self.assertEqual(result["reconciliation"], check)
        failure = {"checked_at": check["checked_at"], "error": "request_failed"}
        self.assertEqual(self.report([], pr_history={"reconciliation": failure})["reconciliation"], failure)

    def test_improvements_validation_and_projection_allowlist(self):
        entry = {"date": "2026-01-25", "happened": "A correction", "links": [{"label": "Evidence", "url": pr(1)["url"]}],
                 "change": "Updated checks", "moves": "Merge rate"}
        result = self.report([], improvements={"schema": 1, "entries": [{**entry, "private": "hidden"}]})
        self.assertEqual(result["improvements"], [entry])
        bad = [None, {"schema": 2, "entries": []}, {"schema": 1, "entries": [entry] * 21},
               {"schema": 1, "entries": [{**entry, "date": "2026-02-30"}]},
               {"schema": 1, "entries": [{**entry, "happened": 5}]},
               {"schema": 1, "entries": [{**entry, "links": [{"label": "API", "url": "https://api.github.com/repos/example/project"}]}]}]
        for value in bad:
            with self.subTest(value=value):
                result = self.report([], improvements=value)
                self.assertEqual(result["improvements"], [])
                self.assertEqual(result["input_quality"]["invalid_improvements"], 1)

    def test_improvement_relative_links_resolve_only_against_render_configuration(self):
        entry = {'date': '2026-01-25', 'happened': 'A correction', 'change': 'Updated checks', 'moves': 'Merge rate',
                 'links': [{'label': 'direction', 'path': 'docs/direction.md'}]}
        result = self.report([], improvements={'schema': 1, 'entries': [entry]})
        self.assertEqual(result['improvements'], [entry])
        config = {**CONFIG, 'public': {'repository': 'other/public', 'dashboard_url': 'https://other.github.io/public/'}}
        page = metrics_site.render(result, config=config)
        self.assertIn('href="https://github.com/other/public/blob/HEAD/docs/direction.md">direction</a>', page)

    def test_improvement_links_reject_ambiguous_missing_absolute_and_parent_paths(self):
        invalid = [{'label': 'direction'}, {'label': 'direction', 'path': 'docs/direction.md', 'url': pr(1)['url']}]
        invalid += [{'label': 'direction', 'path': path} for path in ('/docs/a.md', '../a.md', 'docs/../a.md',
                    'docs/a..md', '//example/a', 'https://example.test/a', '', None, 'docs\\..\\a.md')]
        for link in invalid:
            with self.subTest(link=link):
                entry = {'date': '2026-01-25', 'happened': 'A correction', 'change': 'Updated checks', 'moves': 'Merge rate', 'links': [link]}
                result = self.report([], improvements={'schema': 1, 'entries': [entry]})
                self.assertEqual(result['improvements'], [])
                self.assertEqual(result['input_quality']['invalid_improvements'], 1)


class ActivityTests(unittest.TestCase):
    def project(self, rows, now=NOW, **fields):
        return metrics.activity_projection(rows, now, **fields)

    def test_local_day_changes_at_sixteen_utc_but_cohort_keeps_utc_dates(self):
        rows = [pr(1, submitted="2026-01-11T15:59:59Z"), pr(2, submitted="2026-01-11T16:00:00Z")]
        result = metrics.compute({"prs": rows}, CONFIG, NOW)
        days = {r["date"]: r for r in result["activity"]["days"]}
        self.assertEqual((days["2026-01-11"]["opened"], days["2026-01-12"]["opened"]), (1, 1))
        self.assertEqual(result["cohort"]["n"], 2)
        before = self.project([], NOW.replace(hour=15, minute=59, second=59))
        after = self.project([], NOW.replace(hour=16))
        self.assertEqual((before["today"]["date"], after["today"]["date"]), ("2026-01-25", "2026-01-26"))

    def test_each_event_uses_its_own_time_and_merge_is_never_a_close(self):
        rows = [pr(1, submitted="2026-01-02T15:59:59Z", merged="2026-01-04T16:00:00Z", closed_at="2026-01-04T16:00:00Z"),
                pr(2, "closed", submitted="2026-01-03T16:00:00Z", closed_at="2026-01-06T15:59:59Z")]
        result = self.project(rows)
        days = {r["date"]: r for r in result["days"]}
        self.assertEqual(days["2026-01-02"]["opened"], 1)
        self.assertEqual(days["2026-01-04"]["opened"], 1)
        self.assertEqual(days["2026-01-05"]["merged"], 1)
        self.assertEqual(days["2026-01-06"]["closed_unmerged"], 1)
        self.assertEqual(sum(r["closed_unmerged"] for r in days.values()), 1)
        self.assertEqual(result["totals"], {"opened": 2, "merged": 1, "closed_unmerged": 1, "open": 0,
                                           "first_opened_local_date": "2026-01-02", "projects_with_merges": 1})

    def test_missing_times_do_not_borrow_submission_or_last_check(self):
        result = self.project([pr(1, submitted=None, merged=None, last_check=NOW.isoformat()),
                               pr(2, "closed", submitted="2026-01-05", last_check=NOW.isoformat())])
        self.assertEqual(result["missing"], {"opened_at": 2, "merged_at": 1, "closed_at": 1})
        self.assertEqual(sum(sum(r[k] for k in ("opened", "merged", "closed_unmerged")) for r in result["days"]), 0)
        self.assertEqual((result["totals"]["opened"], result["totals"]["merged"], result["totals"]["closed_unmerged"]), (2, 1, 1))
        self.assertTrue(result["reconciliation"]["ok"])

    def test_cached_event_times_override_ledger_guesses(self):
        row = pr(1, "closed", submitted="2026-01-20T00:00:00Z")
        saved = {"schema_version": 1, "prs": {row["url"]: {"status": "available", "state": "closed",
                 "created_at": "2026-01-03T16:00:00Z", "closed_at": "2026-01-04T16:00:00Z", "fetched_at": NOW.isoformat()}}}
        result = self.project([row], history=saved)
        self.assertEqual(result["totals"]["first_opened_local_date"], "2026-01-04")
        self.assertEqual(result["missing"]["closed_at"], 0)
        self.assertEqual(result["as_of"], NOW.isoformat(timespec="seconds"))
        self.assertFalse(result["observation"]["ok"])

    def test_reopen_counts_only_the_current_terminal_outcome(self):
        events = [{"event": "closed", "created_at": "2026-01-07T00:00:00Z"},
                  {"event": "reopened", "created_at": "2026-01-08T00:00:00Z"}] * 2
        result = self.project([pr(1, "open", closed_at="2026-01-07T00:00:00Z", events=events),
                               pr(2, "closed", closed_at="2026-01-09T16:00:00Z", events=events),
                               pr(3, merged="2026-01-10T16:00:00Z", closed_at="2026-01-10T16:00:00Z", events=events)])
        self.assertEqual(result["reopened"], 3)
        self.assertEqual((result["totals"]["merged"], result["totals"]["closed_unmerged"], result["totals"]["open"]), (1, 1, 1))
        days = {r["date"]: r for r in result["days"]}
        self.assertEqual(days["2026-01-07"]["closed_unmerged"], 0)
        self.assertEqual(days["2026-01-10"]["closed_unmerged"], 1)
        self.assertEqual(days["2026-01-11"]["merged"], 1)

    def test_replacement_is_one_contribution_but_two_cohort_pull_requests(self):
        rows = [pr(1, "closed", closed_at="2026-01-06T00:00:00Z"),
                pr(2, submitted="2026-01-07T00:00:00Z", merged="2026-01-08T00:00:00Z")]
        pair = {"old": pr(1)["url"], "new": pr(2)["url"], "source": pr(2)["url"]}
        result = metrics.compute({"prs": rows, "outcomes": {"schema": 2, "replacements": [pair]}}, CONFIG, NOW)
        activity = result["activity"]
        self.assertEqual(activity["replacements"], {"pairs": 1})
        self.assertEqual((activity["totals"]["opened"], activity["totals"]["merged"], activity["totals"]["closed_unmerged"]), (1, 1, 0))
        self.assertEqual(sum(r["opened"] for r in activity["days"]), 1)
        self.assertTrue(activity["reconciliation"]["ok"])
        self.assertEqual((result["cohort"]["n"], result["cohort"]["closed"]), (2, 1))

    def test_replacement_chains_deduplicate_each_link(self):
        rows = [pr(1, "closed", closed_at="2026-01-06T00:00:00Z"),
                pr(2, "closed", submitted="2026-01-07T00:00:00Z", closed_at="2026-01-08T00:00:00Z"),
                pr(3, "open", submitted="2026-01-09T00:00:00Z")]
        pairs = [{"old": pr(n)["url"], "new": pr(n + 1)["url"], "source": "Recorded discussion"} for n in (1, 2)]
        result = self.project(rows, replacements=pairs)
        self.assertEqual(result["replacements"]["pairs"], 2)
        self.assertEqual((result["totals"]["opened"], result["totals"]["closed_unmerged"], result["totals"]["open"]), (1, 0, 1))
        self.assertTrue(result["reconciliation"]["ok"])

    def test_invalid_replacements_are_exposed_and_not_subtracted(self):
        pair = {"old": pr(1)["url"], "new": pr(2)["url"], "source": pr(2)["url"]}
        for rows, pairs in (([pr(1), pr(2)], [pair]), ([pr(1, "closed"), pr(2)], [pair]),
                            ([pr(1, "closed", closed_at="2026-01-07T00:00:00Z"), pr(2)], [pair]),
                            ([pr(1), pr(2)], [{**pair, "new": "https://github.com/example/other/pull/2"}]),
                            ([pr(1), pr(2)], [{**pair, "source": ""}])):
            result = self.project(rows, replacements=pairs)
            self.assertEqual(result["replacements"]["pairs"], 0)
            self.assertEqual(result["reconciliation"]["invalid_replacements"], 1)
            self.assertFalse(result["reconciliation"]["ok"])
            self.assertEqual(result["totals"]["opened"], 2)

    def test_compute_reports_malformed_replacements_in_activity_reconciliation(self):
        pair = {"old": pr(1)["url"], "new": pr(2)["url"], "source": ""}
        for pairs in ([pair], "bad", [None], None):
            with self.subTest(pairs=pairs):
                data = metrics.compute({"prs": [pr(1), pr(2)], "outcomes": {"schema": 2, "replacements": pairs}}, CONFIG, NOW)
                self.assertEqual(data["input_quality"]["invalid_outcome_rows"], 1)
                self.assertEqual(data["activity"]["reconciliation"]["invalid_replacements"], 1)
                self.assertFalse(data["activity"]["reconciliation"]["ok"])

    def test_compute_rejects_damaged_saved_activity_instead_of_successful_zero(self):
        cases = [([{}], "invalid_rows"), ([pr(1), pr(1)], "duplicate_rows"), ([], "population_difference"),
                 ([pr(2)], "population_difference"), ([pr(1, "invalid")], "invalid_rows"),
                 ([{**pr(1), "events": "bad"}], "invalid_rows")]
        for rows, field in cases:
            with self.subTest(rows=rows):
                observed = {"checked_at": NOW.isoformat(), "prs": rows}
                saved = {"schema_version": 1, "prs": {}, "reconciliation": {"activity": observed}}
                data = metrics.compute({"prs": [pr(1)], "pr_history": saved}, CONFIG, NOW)
                result = data["activity"]
                self.assertFalse(result["observation"]["ok"])
                self.assertEqual(result["observation"]["error"], "invalid_response")
                self.assertIsNone(result["as_of"])
                self.assertEqual(result["totals"]["opened"], 1)
                self.assertGreater(result["reconciliation"][field], 0)
                self.assertFalse(result["reconciliation"]["ok"])

    def test_saved_population_does_not_change_when_ledger_adds_a_new_pr(self):
        observed = {"checked_at": "2026-01-23T16:00:00Z", "prs": [pr(1)], "ledger_urls": [pr(1)["url"]]}
        saved = {"schema_version": 1, "prs": {}, "reconciliation": {"activity": observed}}
        data = metrics.compute({"prs": [pr(1), pr(2)], "pr_history": saved}, CONFIG, NOW)
        self.assertEqual(data["activity"]["totals"]["opened"], 1)
        self.assertEqual(data["activity"]["as_of_local_date"], "2026-01-24")
        self.assertTrue(data["activity"]["observation"]["ok"])
        self.assertTrue(data["activity"]["reconciliation"]["ok"])

    def test_saved_population_manifest_is_checked_even_after_refresh_failure(self):
        observed = {"checked_at": NOW.isoformat(), "prs": [], "ledger_urls": [pr(1)["url"]], "error": "request_failed"}
        data = self.project([pr(1)], observation=observed)
        self.assertFalse(data["observation"]["ok"])
        self.assertEqual(data["observation"]["error"], "invalid_response")
        self.assertEqual(data["reconciliation"]["population_difference"], 1)
        self.assertEqual(data["totals"]["opened"], 1)
        self.assertIsNone(data["as_of"])

    def test_window_is_zero_filled_and_today_is_partial(self):
        result = self.project([pr(1, "open", submitted="2025-01-01T00:00:00Z")])
        self.assertEqual(len(result["days"]), 28)
        self.assertEqual((result["days"][0]["date"], result["days"][-1]["date"]), ("2025-12-29", "2026-01-25"))
        self.assertEqual(result["today"], {"date": "2026-01-25", "opened": 0, "merged": 0, "closed_unmerged": 0, "partial": True})
        self.assertEqual(result["totals"]["opened"], 1)
        self.assertEqual(sum(r["opened"] for r in result["days"]), 0)
        self.assertIsNone(result["as_of"])
        self.assertFalse(result["observation"]["ok"])

    def test_failed_check_keeps_last_good_counts_and_local_window_without_mutation(self):
        observed = {"checked_at": "2026-01-23T16:00:00Z", "prs": [pr(1)]}
        original = json.dumps(observed, sort_keys=True)
        good = self.project([], observation=observed)
        failed = self.project([pr(2), pr(3)], now=NOW.replace(day=28), observation={**observed, "error": "request_failed"})
        self.assertEqual({k: v for k, v in failed.items() if k != "observation"}, {k: v for k, v in good.items() if k != "observation"})
        self.assertEqual(failed["observation"], {"ok": False, "checked_at": "2026-01-23T16:00:00+00:00", "error": "request_failed"})
        self.assertEqual(failed["as_of_local_date"], "2026-01-24")
        self.assertEqual(json.dumps(observed, sort_keys=True), original)
        self.assertTrue(good["observation"]["ok"])
        hidden = self.project([], observation={**observed, "error": "private failure body"})
        self.assertNotIn("private", json.dumps(hidden))

    def test_compute_uses_saved_activity_after_failure_and_handles_legacy_cache_shapes(self):
        observed = {"checked_at": "2026-01-23T16:00:00Z", "prs": [pr(1)], "error": "request_failed"}
        saved = {"schema_version": 1, "prs": {}, "reconciliation": {"activity": observed, "error": "request_failed", "checked_at": NOW.isoformat()}}
        data = metrics.compute({"prs": [pr(1), pr(2)], "pr_history": saved}, CONFIG, NOW)
        self.assertEqual(data["activity"]["totals"]["opened"], 1)
        self.assertEqual(data["activity"]["observation"]["checked_at"], "2026-01-23T16:00:00+00:00")
        self.assertFalse(data["activity"]["observation"]["ok"])
        for cache in ([], None, {"reconciliation": "bad"}):
            result = metrics.compute({"prs": [pr(1)], "pr_history": cache}, CONFIG, NOW)
            self.assertFalse(result["activity"]["observation"]["ok"])
            self.assertIsNone(result["activity"]["as_of"])
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.project([], now=NOW.replace(tzinfo=None))

    def test_reconciliation_exposes_unclassified_states_instead_of_inventing_open(self):
        result = self.project([pr(1), pr(2, "unclassified")])
        self.assertEqual(result["reconciliation"], {"ok": False, "opened": 2, "outcomes": 1, "difference": 1,
                                                     "unclassified": 1, "invalid_replacements": 0, "invalid_rows": 0, "duplicate_rows": 0, "population_difference": 0})
        self.assertEqual(result["totals"]["open"], 0)

    def test_duplicate_urls_and_repository_case_do_not_inflate_daily_counts(self):
        row = pr(1)
        result = self.project([row, {**row, "url": row["url"].replace("example/project", "EXAMPLE/PROJECT") + "/"}])
        self.assertEqual(result["totals"]["opened"], 1)
        self.assertEqual(result["totals"]["projects_with_merges"], 1)


class MetricsFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.put("factory.config.json", CONFIG)
        self.put("ledger/prs.json", [pr(1)])
        self.put("runs/2026-01-04/candidate-1/item.json", item(1)["meta"])

    def put(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_reader_limits_itself_to_canonical_records_and_ignores_broken_files(self):
        self.put("runs/2026-01-04/candidate-1/artifacts/repro/item.json", {"created_at": "2026-01-01"})
        self.put("runs/2026-01-04/candidate-1/qualify.json", []).write_text("{")
        self.put("runs/2026-01-04/candidate-1/value-review.json", [])
        self.put("runs/2026-01-04/candidate-1/receipts/one.json", {"duration_s": 3})
        self.put("ledger/followup-events.jsonl", {}).write_text('{"kind":"ci-failing"}\nbroken\n42\n')
        self.put("docs/owner-actions.md", {}).write_text('| 2026-01-05 | request |\n| 2026-01-06 | resolved |\n')
        self.put("workspace/shift-logs/one/summary.json", {"status": "exited 0"})
        self.put("workspace/shift-logs/two/summary.json", [])
        self.put("runs/2026-01-04/product/item.json", {"line": "product"})
        with patch.object(metrics, "history", return_value=None):
            result = metrics.read_snapshot(self.root, CONFIG)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(result["items"][0]["receipts"]), 1)
        self.assertEqual(len(result["shifts"]), 1)
        self.assertEqual(result["owner_action_rows"], 2)
        self.assertEqual(result["input_quality"]["invalid_rows"], 2)
        self.assertEqual(result["input_quality"]["unreadable_files"], 3)

    def test_outcomes_are_loaded_beside_configured_ledger_without_writing(self):
        config = {**CONFIG, "ledger_file": "custom/prs.json"}
        self.put("custom/prs.json", [pr(1, "closed")])
        missing = metrics.read_snapshot(self.root, config)
        self.assertEqual(missing["outcomes"], {"schema": 1, "closures": {}, "maintainer_edits": {}})
        self.assertFalse((self.root / "custom/pr-outcomes.json").exists())
        data = {"schema": 1, "closures": {pr(1)["url"]: {"reason": "withdrawn", "evidence": pr(1)["url"], "note": "private note"}}}
        path = self.put("custom/pr-outcomes.json", data)
        before = path.read_bytes()
        saved = metrics.read_snapshot(self.root, config)
        self.assertEqual(saved["outcomes"], data)
        result = metrics.compute(saved, CONFIG, NOW)
        self.assertEqual(result["pull_requests"][0]["reason"], "withdrawn")
        self.assertEqual(path.read_bytes(), before)
        path.write_text("broken")
        result = metrics.compute(metrics.read_snapshot(self.root, config), CONFIG, NOW)
        self.assertEqual(result["input_quality"]["invalid_outcome_rows"], 1)
        self.assertEqual(result["pull_requests"][0]["reason"], "unclassified")

    def test_review_reader_understands_legacy_archives_and_schema_two_renderings(self):
        folder = self.root / "runs/2026-01-04/candidate-1"
        self.put(str(folder.relative_to(self.root) / "review.md"), {}).write_text('head: aaaaaaa\nverdict: ship\n')
        self.put(str(folder.relative_to(self.root) / "review-round1.md"), {}).write_text('head: bbbbbbb\nverdict: **fix-needed**\n')
        self.put(str(folder.relative_to(self.root) / "review-round2.md"), {}).write_text('head: aaaaaaa\nverdict: ship\n')
        self.put(str(folder.relative_to(self.root) / "review-round1.json"), {"verdict": "ship", "head": "bbbbbbb", "complete": False})
        self.put(str(folder.relative_to(self.root) / "review.json"), {"verdict": "ship", "head": "aaaaaaa", "complete": True})
        self.put(str(folder.relative_to(self.root) / "reviewer-probe.json"), {"verdict": "fix-needed"})
        reader = metrics.Reader()
        self.assertEqual(len(reader.reviews(folder, 1)), 2)
        self.assertEqual(len(reader.reviews(folder, 2)), 1)
        # Legacy items may retain JSON-only rounds from a later reviewer.
        self.put(str(folder.relative_to(self.root) / "review-round3.json"), {"head": "ccccccc", "verdict": "fix-needed", "complete": True})
        self.assertEqual(len(reader.reviews(folder, 1)), 3)
        self.assertEqual(len(reader.reviews(folder, None)), 3)
        self.assertEqual(len(reader.reviews(folder, 2)), 2)

    def test_nested_legacy_items_include_lead_time_and_reviews_without_embedded_fixtures(self):
        nested = "runs/2026-01-04/legacy-group/candidate-2"
        meta = {"line": "oss", "created_at": "2026-01-04T12:00:00Z"}
        self.put(f"{nested}/item.json", meta)
        self.put(f"{nested}/review-round1-abcdef0.md", {}).write_text("head: bbbbbbb\nverdict: fix-needed\n")
        self.put(f"{nested}/review.md", {}).write_text("head: aaaaaaa\nverdict: ship\n")
        self.put("runs/2026-01-04/candidate-1/review.md", {}).write_text("head: ccccccc\nverdict: ship\n")
        self.put(f"{nested}/artifacts/fixture/item.json", meta)
        self.put("runs/2026-01-04/fixtures/example/item.json", meta)
        outside = self.put("elsewhere/item.json", meta)
        (self.root / "runs/2026-01-04/linked-outside").symlink_to(outside.parent, target_is_directory=True)
        (self.root / "runs/2026-01-04/alias").symlink_to(self.root / nested, target_is_directory=True)
        self.put("ledger/prs.json", [pr(1), pr(2, item=nested), pr(3, item="../elsewhere")])
        saved = metrics.read_snapshot(self.root, CONFIG)
        self.assertEqual(len(saved["items"]), 2)
        result = metrics.compute(saved, CONFIG, NOW)["metrics"]
        self.assertEqual(result["candidates_found"]["value"], 2)
        self.assertEqual(result["median_lead_hours"]["value"], 18)
        self.assertEqual(result["median_lead_hours"]["coverage"], {"covered": 2, "total": 3})
        self.assertEqual(result["independent_review_rounds_per_pr"]["value"], 1.5)
        self.assertEqual(result["independent_review_rounds_per_pr"]["coverage"], {"covered": 2, "total": 3})
        self.assertEqual(result["sent_back_review"]["value"], 1)

    def test_numbered_archive_suffixes_match_canonical_rounds_and_deduplicate_renderings(self):
        from types import SimpleNamespace
        from factory.core import review

        folder = self.root / "runs/2026-01-04/candidate-1"
        name = folder.relative_to(self.root)
        for suffix, head, verdict in (("round1-abcdef0", "aaaaaaa", "fix-needed"),
                                      ("round2-commitmsg", "bbbbbbb", "fix-needed"),
                                      ("round3", "ccccccc", "ship")):
            self.put(str(name / f"review-{suffix}.md"), {}).write_text(f"head: {head}\nverdict: {verdict}\n")
            self.put(str(name / f"review-{suffix}.json"), {"head": head, "verdict": verdict, "complete": True})
        self.put(str(name / "review.md"), {}).write_text("head: ccccccc\nverdict: ship\n")
        self.put(str(name / "review.json"), {"head": "ccccccc", "verdict": "ship", "complete": True})
        self.put(str(name / "review-probe.json"), {"head": "ddddddd", "verdict": "fix-needed", "complete": True})
        for schema in (1, 2):
            with self.subTest(schema=schema):
                rows = metrics.Reader().reviews(folder, schema)
                canonical = review._round_records(SimpleNamespace(dir=folder, meta={"schema": schema}))
                self.assertEqual({(r["head"], r["verdict"]) for r in rows},
                                 {(r["head"], r["verdict"]) for r in canonical})
                self.assertEqual(len(rows), 3)
                result = metrics.compute({"prs": [pr(1)], "items": [item(1, reviews=rows)]}, CONFIG, NOW)["metrics"]
                self.assertEqual(result["independent_review_rounds_per_pr"]["value"], 3)
                self.assertEqual(result["sent_back_review"]["value"], 1)

    def test_record_root_configuration_resolves_shift_logs_and_local_overrides(self):
        other = self.root / "other"
        self.put(".factory.local.json", {"repos_dir": str(other / "repos"), "limits": {"one": 1}})
        self.put("other/shift-logs/one/summary.json", {"status": "exited 0"})
        cfg = load_config(self.root)
        result = metrics.read_snapshot(self.root, cfg)
        self.assertEqual(len(result["shifts"]), 1)
        self.assertEqual(cfg["limits"], {"one": 1})

    def test_missing_or_malformed_ledger_is_a_fatal_input_error(self):
        (self.root / "ledger/prs.json").write_text("broken")
        with self.assertRaises(CommandError):
            metrics.read_snapshot(self.root, CONFIG)

    def test_cli_reads_records_without_mutation_and_writes_json_and_static_site(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        output, site = self.root / "output/metrics.json", self.root / "output/site"
        with patch.dict(metrics.CONFIG, CONFIG), redirect_stdout(io.StringIO()) as stdout:
            result = cli.main(["metrics", "--records-root", str(self.root), "-o", str(output), "--site", str(site)])
        self.assertEqual(result, 0)
        self.assertIn("PRs merged", stdout.getvalue())
        self.assertEqual(json.loads(output.read_text())["schema_version"], 4)
        self.assertTrue((site / "index.html").exists())
        self.assertTrue((site / "board.html").exists())
        self.assertEqual(len(list(site.glob('*.svg'))), 12)
        for path in site.glob('*.svg'):
            self.assertEqual(ET.fromstring(path.read_text()).tag, "{http://www.w3.org/2000/svg}svg")
        for name, contents in before.items():
            self.assertEqual(pathlib.Path(name).read_bytes(), contents)

    def test_privacy_scan_failure_writes_neither_json_nor_site(self):
        output, site = self.root / "out.json", self.root / "site"
        with patch.dict(metrics.CONFIG, CONFIG), patch.object(leaks, "scan", return_value=["blocked"]), redirect_stdout(io.StringIO()):
            result = cli.main(["metrics", "--records-root", str(self.root), "--output", str(output), "--site", str(site)])
        self.assertEqual(result, 3)
        self.assertFalse(output.exists())
        self.assertFalse(site.exists())

    def test_public_files_include_both_scanned_pages_and_all_figures(self):
        with patch.object(metrics, "scan_output") as scan:
            files = metrics.public_files(self.root, config=CONFIG)
        names = {f'site/{name}-{theme}.svg' for theme in ('light', 'dark') for name in ('line', 'gate', 'board', 'profile', 'activity')}
        names.update(f'site/profile-{theme}-compact.svg' for theme in ('light', 'dark'))
        self.assertEqual(set(files), {"site/index.html", "site/board.html", "site/metrics.json"} | names)
        self.assertEqual([call.args[0] for call in scan.call_args_list], list(files.values()))
        self.assertIn("author%3Aexample", files["site/board.html"])

    def test_site_writers_preserve_the_complete_filename_content_mapping(self):
        data = metrics.compute(snapshot(), CONFIG, NOW)
        figures = {'first.svg': 'FIRST FIGURE', 'second.svg': 'SECOND FIGURE'}
        assets = {'index.html': 'LANDING PAGE', 'board.html': 'STATUS BOARD', **figures}
        encoded = json.dumps(data, indent=2) + '\n'
        output, site = self.root / 'output/metrics.json', self.root / 'output/site'
        with patch.dict(metrics.CONFIG, CONFIG), patch.object(metrics, 'compute', return_value=data), \
                patch.object(metrics_site, 'render', return_value=assets['index.html']), \
                patch.object(metrics_site, 'render_board', return_value=assets['board.html']), \
                patch.object(metrics_site, 'figures', return_value=figures), \
                patch.object(metrics, 'scan_output'), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['metrics', '--records-root', str(self.root), '--output', str(output),
                                       '--site', str(site)]), 0)
            files = metrics.public_files(self.root, config=CONFIG)
        self.assertEqual({p.name: p.read_text() for p in site.iterdir()}, assets)
        self.assertEqual(output.read_text(), encoded)
        self.assertEqual(files, {'site/' + name: text for name, text in
                                 {'metrics.json': encoded, **assets}.items()})

    def test_each_site_asset_is_scanned_before_any_output_is_written(self):
        output, site = self.root / "out.json", self.root / "site"
        data = metrics.compute(snapshot(), CONFIG, NOW)
        assets = {'index.html': metrics_site.render(data, CONFIG), 'board.html': metrics_site.render_board(data, CONFIG),
                  **metrics_site.figures(data)}
        for rejected in assets.values():
            def scan(text):
                if text == rejected:
                    raise CommandError("asset blocked")
            with patch.dict(metrics.CONFIG, CONFIG), patch.object(metrics, 'compute', return_value=data), \
                    patch.object(metrics, "scan_output", side_effect=scan), redirect_stdout(io.StringIO()):
                result = cli.main(["metrics", "--records-root", str(self.root), "--output", str(output), "--site", str(site)])
            self.assertEqual(result, 3)
            self.assertFalse(output.exists())
            self.assertFalse(site.exists())

    def test_json_output_cannot_overwrite_any_site_asset(self):
        names = ['index.html', 'board.html', *metrics_site.figures(metrics.compute(snapshot(), CONFIG, NOW))]
        site = self.root / 'site'
        for name in names:
            with self.subTest(name=name), patch.dict(metrics.CONFIG, CONFIG), patch.object(metrics, 'scan_output'), redirect_stdout(io.StringIO()):
                result = cli.main(["metrics", "--records-root", str(self.root), "--output", str(site / name), "--site", str(site)])
            self.assertEqual(result, 3)
            self.assertFalse(site.exists())

    def test_card_prints_the_same_projection_without_writes_or_network(self):
        data = metrics.compute(snapshot(), CONFIG, NOW)
        history = self.put('history.json', {'schema_version': 1, 'prs': {}})
        suite = self.put('suite.json', snapshot()['suite'])
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        options = ['--records-root', str(self.root), '--history-cache', str(history), '--suite-report', str(suite)]
        for arguments in (['metrics', 'card', *options], ['metrics', *options, 'card']):
            with patch.dict(metrics.CONFIG, CONFIG), patch.object(metrics, 'compute', return_value=data) as compute, \
                    patch.object(metrics, 'read_snapshot', wraps=metrics.read_snapshot) as read, \
                    patch.object(metrics_site, 'status_card', wraps=metrics_site.status_card) as card, \
                    patch.object(metrics, 'scan_output') as scan, patch.object(metrics, 'write_json') as write, \
                    patch('factory.core.metrics_history.api', side_effect=AssertionError('no network')), \
                    redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(cli.main(arguments), 0)
            read.assert_called_once_with(self.root.resolve(), load_config(self.root), str(suite), str(history))
            compute.assert_called_once()
            card.assert_called_once_with(data)
            scan.assert_called_once_with(metrics_site.status_card(data))
            write.assert_not_called()
            self.assertEqual(stdout.getvalue(), metrics_site.status_card(data))
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)

    def test_card_scan_failure_does_not_print_the_card(self):
        with patch.dict(metrics.CONFIG, CONFIG), patch.object(metrics, 'scan_output', side_effect=CommandError('card blocked')), \
                redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(cli.main(['metrics', 'card', '--records-root', str(self.root)]), 3)
        self.assertNotIn('software-factory', stdout.getvalue())

    def test_git_history_only_counts_explicit_nonmerge_repairs_without_exporting_subjects(self):
        response = type("Result", (), {"returncode": 0, "stdout": "fix: repair parser\nFix edge case\nFeature work\n"})()
        with patch.object(metrics.subprocess, "run", return_value=response) as run:
            result = metrics.history(self.root)
        self.assertEqual(result, {"commits": 3, "fix_commits": 2})
        self.assertIn("--no-merges", run.call_args.args[0])


class MetricsSiteTests(unittest.TestCase):
    def report(self):
        rows = [pr(n) for n in range(1, 23)] + [pr(23, "closed"), pr(24, "silent")]
        data = metrics.compute({**snapshot(), "prs": rows, "pr_history": CohortTests().histories(rows)}, CONFIG, NOW)
        data["cohort"].update(merged_no_rework=20, merged_reworked=2, maintainer_edits_neutral=3)
        data["metrics"]["merged_no_rework_share"].update(value=.909, numerator=20)
        data["reasons"]["merged_reworked"] = [{"key": "author-update", "n": 2, "pull_requests": [pr(1)["url"], pr(2)["url"]]}]
        data["improvements"] = [{"date": "2026-01-24", "happened": "A correction", "change": "Updated checks",
                                 "moves": "Merge rate", "links": [{"label": "project#1", "url": pr(1)["url"]}]}]
        return data

    def render(self, data=None):
        return metrics_site.render_board(self.report() if data is None else data, config=CONFIG)

    def test_board_keeps_the_projection_and_metric_definitions(self):
        data = self.report()
        page = self.render(data)
        embedded = re.search(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', page, re.S)[1]
        self.assertEqual(json.loads(embedded), data)
        for row in data["metrics"].values():
            for field in ("label", "definition", "source", "reason", "note"):
                if row.get(field):
                    self.assertTrue(metrics_site.escape(row[field]) in page, (row['label'], field))

    def test_rate_formatting_keeps_counts_and_intervals(self):
        data = self.report()['metrics']
        self.assertEqual(metrics_site.rate_text(data['cohort_merge_rate']), '92%')
        self.assertEqual(metrics_site.rate_text(data['merged_no_rework_share']), '91%')
        self.assertEqual(metrics_site.rate_detail(data['cohort_merge_rate'], merge=True),
                         '22 of 24 merged · 95% interval 74 to 98%')
        self.assertEqual(metrics_site.rate_detail(data['merged_no_rework_share']),
                         '20 of 22 merges: no change request, no fix from the factory, no repair by a maintainer')

    def test_low_sample_uses_each_rates_counts_not_percentage(self):
        data = metrics.compute(snapshot(), CONFIG, NOW)['metrics']
        self.assertEqual(metrics_site.rate_text(data['cohort_merge_rate']), '2 of 4')
        self.assertEqual(metrics_site.rate_text(data['merged_no_rework_share']), '0 of 0')
        row = {**self.report()['metrics']['merged_no_rework_share'], 'value': None, 'numerator': 18, 'denominator': 19}
        self.assertEqual(metrics_site.rate_text(row), '18 of 19')

    def test_verification_links_use_configured_user_and_cutoff_not_last_opened(self):
        from html import unescape

        page = self.render()
        base = 'is:pr author:example -user:example created:2026-01-05..2026-01-11'
        for label, suffix in (("opened", ""), ("merged", " is:merged"), ("closed", " is:closed is:unmerged"), ("still open", " is:open")):
            url = re.search(r'href="([^"]+)">' + label + '</a>', page)[1]
            self.assertEqual(urlsplit(url).path, '/search')
            self.assertEqual(parse_qs(urlsplit(unescape(url)).query), {"q": [base + suffix], "type": ["pullrequests"]})
        self.assertIn('href="https://github.com/example/project/pulls?q=is%3Apr+author%3Aexample"', page)
        self.assertIn('href="https://github.com/example/factory/blob/HEAD/docs/metrics.md"', page)
        url = re.search(r'href="([^"]+)">GitHub\'s own search</a>', page)[1]
        self.assertEqual(urlsplit(url).path, '/search')
        self.assertEqual(parse_qs(urlsplit(unescape(url)).query),
                         {'q': ['is:pr author:example -user:example created:>=2026-01-05'], 'type': ['pullrequests']})

    def test_reason_rows_put_unclassified_last_and_bound_the_link_list(self):
        rows = [{"key": "unclassified", "n": 5, "pull_requests": [pr(n)["url"] for n in range(1, 6)]},
                {"key": "duplicate", "n": 2, "pull_requests": [pr(6)["url"], pr(7)["url"]]}]
        text = metrics_site.reason_rows(rows)
        self.assertLess(text.index('Someone else fixed it first'), text.index('Not classified yet'))
        self.assertIn(pr(6)['url'], text)
        self.assertNotIn(pr(1)['url'], text)

    def test_all_reason_and_outcome_labels_are_fixed(self):
        labels = {
            'author-update': ('The factory changed it after opening', 'Usually after a review comment, often from a bot. Owned by the required checks and independent review.'),
            'change-requested': ('A maintainer formally requested changes', 'Owned by independent review.'),
            'maintainer-repair': ('A maintainer repaired it before merging', 'Owned by build and independent review.'),
            'no-real-user': ('No real-world case shown', 'Owned by value review.'),
            'unchecked': ('Not checked yet', 'Follow-up has not read this thread yet.'),
            'works-as-designed': ('Works as designed', 'Owned by value review.'),
            'approach-declined': ('Approach declined', 'Owned by value review.'),
            'duplicate': ('Someone else fixed it first', 'Owned by qualify.'),
            'superseded': ('The maintainer fixed it another way', 'Owned by build: fix the layer that owns the contract.'),
            'process': ('A project rule was not followed', 'Owned by the repository card.'),
            'withdrawn': ('Withdrawn by the factory', 'Owned by independent review.'),
            'unexplained': ('No reason given', 'No owner.'),
            'unclassified': ('Not classified yet', 'Counts against the factory until someone records a reason.'),
            'no-reply': ('No reply from a person', 'Owned by project selection. The factory has stopped opening pull requests there.'),
            'in-review': ('In review', "Waiting is the maintainers' time. It is not measured."),
            'agreement': ('Waiting on a contributor agreement', 'Owner action.'),
        }
        data = self.report()
        text = metrics_site.reason_rows([{"key": key, "n": 1, "pull_requests": []} for key in labels])
        for label, owner in labels.values():
            self.assertIn(metrics_site.escape(label), text)
            self.assertIn(metrics_site.escape(owner), text)
        for bucket, label in (("merged_no_rework", "Merged without rework"), ("merged_reworked", "Merged after rework"),
                              ("merged_unverified", "Merged, history unavailable"), ("closed", "Closed without merge"), ("open", "Still open")):
            data["pull_requests"][0].update(bucket=bucket, reason="change-requested", evidence=pr(1)["url"] + '#issuecomment-1')
            text = metrics_site.pull_request_rows(data['pull_requests'])
            self.assertIn(label, text)
            self.assertIn('A maintainer formally requested changes', text)
            self.assertIn('href="' + pr(1)["url"] + '#issuecomment-1">evidence</a>', text)

    def test_last_opened_sentence_is_data_driven_and_omitted_within_seven_days(self):
        data = self.report()
        self.assertIn('No pull request has been opened since 2026-01-05.', self.render(data))
        for date, shown in (("2026-01-17", True), ("2026-01-18", False), ("2026-01-24", False)):
            data["pull_requests"][-1]["opened"] = date
            self.assertEqual('No pull request has been opened since' in self.render(data), shown)
        data["pull_requests"] = []
        self.assertNotIn('No pull request has been opened since', self.render(data))

    def test_improvement_rows_escape_every_field_and_empty_rows_are_omitted(self):
        data = self.report()
        hostile = '</script><img src="x" onerror="alert(1)">&'
        data["improvements"][0] = {key: hostile for key in ("date", "happened", "change", "moves")}
        url = pr(1)["url"] + '?q="<&'
        data["improvements"][0]["links"] = [{"label": hostile, "url": url}, {"label": "unsafe", "url": "javascript:alert(1)"}]
        text = metrics_site.improvement_rows(data['improvements'], 'https://github.com/example/factory')
        self.assertEqual(text.count(metrics_site.escape(hostile)), 5)
        self.assertIn('href="' + metrics_site.escape(url) + '"', text)
        self.assertNotIn('href="javascript:', text)
        self.assertNotIn('<img src=', text)
        self.assertEqual(metrics_site.improvement_rows([], 'https://github.com/example/factory'), '')

    def test_reconciliation_has_matching_different_and_not_checked_states(self):
        for reconciliation, sentence in (({"github": 24, "ledger": 24}, 'On this snapshot both list 24.'),
                                         ({"github": 25, "ledger": 24}, 'On this snapshot GitHub lists 25 and the ledger lists 24.'),
                                         ({"status": "not_checked"}, 'The comparison has not run for this snapshot.'),
                                         ({"error": "request_failed"}, 'The comparison has not run for this snapshot.')):
            self.assertEqual(metrics_site.reconciliation_text(reconciliation), sentence)

    def test_weekly_rows_keep_event_and_opening_cohort_counts_distinct(self):
        from test_metrics_site import Fragment

        rows = [{'week': '2026-01-05', 'opened': 24, 'merged': 11, 'cohort_merged': 22,
                 'cohort_no_rework': 20, 'cohort_closed': 1, 'cohort_open': 1}]
        for keys in (('opened', 'merged'), ('opened', 'cohort_merged', 'cohort_no_rework', 'cohort_closed', 'cohort_open')):
            text = metrics_site.weekly_rows(rows, keys)
            self.assertEqual(Fragment(text).text, ['2026-01-05', *[str(rows[0][key]) for key in keys]])

    def test_inside_facts_use_metrics_and_missing_reasons(self):
        data = self.report()
        facts = metrics_site.fact_values(data['metrics'])
        self.assertEqual(facts['CANDIDATES_VALUE'], '4')
        self.assertEqual(facts['REVIEW_VALUE'], '1')
        self.assertEqual(facts['SHIFTS_VALUE'], '2 of 3')
        self.assertEqual(facts['TESTS_DETAIL'], 'passing in 3 s on the private repository; this snapshot ships a subset')
        for key in ('candidates_found', 'prs_opened', 'candidates_stopped_before_publish', 'sent_back_review',
                    'shift_completion_rate', 'factory_test_count', 'factory_test_runtime'):
            with self.subTest(key=key):
                data = self.report()
                data['metrics'][key].update(value=None, reason='Missing <evidence>')
                values = metrics_site.fact_values(data['metrics']).values()
                self.assertIn('Not measured', values)
                self.assertIn('Missing &lt;evidence&gt;', values)

    def test_inline_json_and_all_interpolated_text_are_escaped(self):
        hostile = '</script><img src="x" onerror="alert(1)">&'
        paths = [('generated_at',), ('schema_version',), ('cohort', 'first_opened'), ('cohort', 'last_opened'),
                 ('pull_requests', 0, 'repository'), ('pull_requests', 0, 'opened'), ('pull_requests', 0, 'number'),
                 ('by_repository', 0, 'repository'), ('weekly', 'rows', 0, 'week'),
                 *[("metrics", "prs_merged", key) for key in ('label', 'definition', 'source', 'status', 'reason', 'note')]]
        for path in paths:
            with self.subTest(path=path):
                data = self.report()
                row = data
                for key in path[:-1]:
                    row = row[key]
                row[path[-1]] = hostile
                page = self.render(data)
                self.assertNotIn('<img src=', page)
                self.assertIn('\\u003c/script\\u003e', page)
                self.assertIn(metrics_site.escape(hostile), page)
        for url in ('javascript:alert(1)', 'https://github.com.evil.test/project', 'https://api.github.com/repos/example/project'):
            data = self.report()
            data["pull_requests"][0].update(url=url, evidence=url)
            page = self.render(data)
            self.assertNotIn('href="' + url, page)

    def test_week_ticks_show_all_through_eight_then_endpoints_and_middle(self):
        for count in range(1, 100):
            with self.subTest(count=count):
                ticks = sorted(metrics_site.date_ticks(count))
                self.assertEqual(ticks, list(range(count)) if count <= 8 else [0, (count - 1) // 2, count - 1])

    def test_empty_chart_is_explicit(self):
        self.assertIn("No dated observations", metrics_site.chart([], "Output", [("opened", "Opened")], "count"))

    def test_figure_rates_respect_each_sample(self):
        data = self.report()
        data['metrics']['merged_no_rework_share'].update(value=None, numerator=18, denominator=19)
        self.assertEqual(metrics_site.figure_rates(metrics_site.figure_data(data)), [('92%', 22, 24), ('18 of 19', 18, 19)])
        data = metrics.compute(snapshot(), CONFIG, NOW)
        self.assertEqual(metrics_site.figure_rates(metrics_site.figure_data(data)), [('2 of 4', 2, 4), ('0 of 0', 0, 0)])

    def test_figures_use_projection_values_and_share_the_line_accessible_name(self):
        data = self.report()
        data['pull_requests'][0]['opened'] = '2025-12-01'
        data['metrics']['candidates_found']['value'] = 99
        values = metrics_site.figure_data(data)
        self.assertEqual((values['as_of'], values['since'], values['candidates']), ('2026-01-25', '2025-12-01', 99))
        for name, svg in metrics_site.figures(data).items():
            root = ET.fromstring(svg)
            label = root.attrib['aria-label']
            self.assertEqual(root.find('{*}title').text, label)
            if name.startswith('line'):
                self.assertEqual(label, metrics_site.line_label(values))
                self.assertIn('of 99 recorded candidates', label)
            if name.startswith('profile'):
                self.assertIn('22 merged pull requests in 1 projects since 2025-12-01', label)

    def test_figures_escape_dates_in_text_and_accessible_names(self):
        data = self.report()
        hostile = '\" onload=\"bad\"><script>&'
        data['cohort']['as_of'] = hostile
        data['pull_requests'][0]['opened'] = hostile
        data['activity']['as_of_local_date'] = hostile
        for name, svg in metrics_site.figures(data).items():
            root = ET.fromstring(svg)
            self.assertNotIn('onload', root.attrib)
            self.assertIsNone(root.find('.//{*}script'))
            if name.startswith('gate-'):
                self.assertEqual(svg, metrics_site.gate(name.removeprefix('gate-').removesuffix('.svg')))
            else:
                self.assertIn(metrics_site.escape(hostile), svg)

    def test_each_missing_figure_count_is_explicit_not_zero(self):
        paths = [('metrics', key, 'value') for key in ('candidates_found', 'candidates_stopped_before_publish',
                 'prs_opened', 'prs_merged', 'prs_closed', 'prs_open')]
        paths += [('population', 'merged_projects')]
        paths += [('cohort', key) for key in ('n', 'merged', 'merged_no_rework', 'merged_reworked', 'closed', 'open')]
        for path in paths:
            with self.subTest(path=path):
                data = self.report()
                target = data
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = None
                values = metrics_site.figures(data)
                affected = [ET.fromstring(svg) for svg in values.values() if 'Counts not measured:' in svg]
                self.assertTrue(affected)
                for root in affected:
                    self.assertIn('n/a', root.attrib['aria-label'])
                    self.assertNotIn('None', ''.join(root.itertext()))
                    self.assertIn('n/a', ''.join(root.itertext()))


if __name__ == "__main__":
    unittest.main()
