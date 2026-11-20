import datetime as dt
import io
import json
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from support import setup_module
from factory import cli
from factory.core import leaks, metrics, metrics_site, privacy
from factory.util import CommandError, load_config


def setUpModule():
    setup_module(unittest)


CONFIG = {"public": {"repository": "example/factory", "dashboard_url": "https://example.github.io/factory/"},
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
            "prs_opened": 4, "prs_merged": 2, "prs_open": 1, "merge_rate": .667,
            "closed_unmerged_share": .333, "median_lead_hours": 24,
            "ci_failures_after_submission": 1, "independent_review_rounds_per_pr": 1.333,
            "candidates_found": 4, "dropped_qualify": 1, "dropped_value": 1, "sent_back_review": 1,
            "coordinator_minutes": 15, "coordinator_tool_calls": 30, "shift_completion_rate": .667,
            "factory_test_count": 4, "factory_test_runtime": 2.5, "factory_fix_commits": 3,
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(rows[key]["value"], value)
        untracked = {"first_round_approval_share", "review_fix_hours", "fixes_pushed_after_review", "candidate_selection_misses",
                     "review_rounds_per_pr", "fixes_pushed_before_review",
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
        for key in ("merge_rate", "first_round_approval_share", "review_fix_hours", "median_lead_hours",
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
                    "merged_needing_fix_share", "response_samples", "no_response", "merge_time_samples"}
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
        self.assertEqual(rows[0]["merge_rate"], .5)
        self.assertEqual(rows[3]["merged"], 1)
        self.assertEqual(rows[2]["open_submission_cohort"], 1)
        self.assertIsNone(rows[1]["merge_rate"])
        self.assertIsNone(rows[1]["median_lead_hours"])
        self.assertEqual(rows[0]["median_lead_hours"], 23)
        self.assertEqual(rows[0]["lead_time_samples"], 2)
        self.assertEqual(rows[2]["median_lead_hours"], 192)
        self.assertIsNone(rows[3]["median_lead_hours"])
        self.assertIsNone(rows[3]["review_fix_hours"])
        self.assertIsNone(rows[3]["first_round_approval_share"])
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
        for key in ("first_round_approval_share", "fixes_pushed_after_review", "review_fix_hours", "candidate_selection_misses",
                    "owner_interventions", "unattended_shifts", "agent_minutes_per_pr", "tool_calls_per_pr"):
            self.assertEqual(rows[key]["status"], "not_tracked", key)
            self.assertEqual(rows[key]["coverage"]["covered"], 0, key)
            self.assertTrue(rows[key]["reason"], key)
        self.assertEqual(rows["first_round_approval_share"]["coverage"]["total"], 4)
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
        for output in (json.dumps(result), metrics_site.render(result)):
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
        self.assertEqual(json.loads(output.read_text())["schema_version"], 3)
        self.assertTrue((site / "index.html").exists())
        for name, contents in before.items():
            self.assertEqual(pathlib.Path(name).read_bytes(), contents)

    def test_privacy_scan_failure_writes_neither_json_nor_site(self):
        output, site = self.root / "out.json", self.root / "site"
        with patch.dict(metrics.CONFIG, CONFIG), patch.object(leaks, "scan", return_value=["blocked"]), redirect_stdout(io.StringIO()):
            result = cli.main(["metrics", "--records-root", str(self.root), "--output", str(output), "--site", str(site)])
        self.assertEqual(result, 3)
        self.assertFalse(output.exists())
        self.assertFalse(site.exists())

    def test_git_history_only_counts_explicit_nonmerge_repairs_without_exporting_subjects(self):
        response = type("Result", (), {"returncode": 0, "stdout": "fix: repair parser\nFix edge case\nFeature work\n"})()
        with patch.object(metrics.subprocess, "run", return_value=response) as run:
            result = metrics.history(self.root)
        self.assertEqual(result, {"commits": 3, "fix_commits": 2})
        self.assertIn("--no-merges", run.call_args.args[0])


class MetricsSiteTests(unittest.TestCase):
    def test_self_contained_accessible_site_has_embedded_json_and_definitions(self):
        data = metrics.compute(snapshot(), CONFIG, NOW)
        page = metrics_site.render(data)
        self.assertIn('<title>Software Factory</title>', page)
        self.assertIn('type="application/json"', page)
        self.assertIn('<svg', page)
        self.assertIn('tabindex="0"', page)
        self.assertIn('View weekly values', page)
        self.assertIn('prefers-color-scheme: dark', page)
        self.assertIn(':root[data-theme="dark"]', page)
        self.assertIn('connect-src \'none\'', page)
        self.assertNotIn('src="http', page)
        self.assertNotIn('fetch(', page)
        self.assertNotIn('localStorage', page)
        self.assertNotIn('@@', page)
        for row in data["metrics"].values():
            self.assertIn(metrics_site.escape(row["definition"]), page)

    def test_headline_and_charts_follow_factory_controlled_contract(self):
        data = metrics.compute(snapshot(), CONFIG, NOW)
        page = metrics_site.render(data)
        headline = page.split('<section class="stats"', 1)[1].split('</section>', 1)[0]
        self.assertIn("First-round approval", headline)
        self.assertIn("Not tracked", headline)
        self.assertIn(metrics_site.escape(data["metrics"]["first_round_approval_share"]["reason"]), headline)
        self.assertIn("Candidate-to-PR lead time", page)
        self.assertNotIn("Time to merge", page)
        self.assertNotIn("Median time to merge", page)
        self.assertNotIn("No maintainer response", page)
        self.assertIn('href="#first-round-quality"', page)
        self.assertIn('id="first-round-quality"', page)
        self.assertIn("schema 3", page)

    def test_inline_json_and_html_are_escaped(self):
        data = metrics.compute(snapshot(), CONFIG, NOW)
        data["metrics"]["prs_merged"]["label"] = '</script><img src="x" onerror="alert(1)">'
        page = metrics_site.render(data)
        self.assertNotIn('<img src=', page)
        self.assertIn('\\u003c/script\\u003e', page)

    def test_line_geometry_uses_hours_not_day_formatted_tick_labels_and_leaves_gaps(self):
        rows = [{"week": "2026-01-05", "latency": 72}, {"week": "2026-01-12", "latency": None},
                {"week": "2026-01-19", "latency": 24}]
        svg = metrics_site.chart(rows, "Hours", [("latency", "Median hours")], "hours")
        self.assertIn('>75</text>', svg)
        self.assertEqual(svg.count('class="series-1 trend"'), 2)
        self.assertNotIn("nan", svg.lower())
        self.assertIn("Not tracked", svg)

    def test_phone_date_ticks_preserve_endpoints_without_crowding(self):
        for count in range(1, 100):
            with self.subTest(count=count):
                ticks = sorted(metrics_site.date_ticks(count))
                self.assertLessEqual(len(ticks), 3)
                self.assertEqual(ticks[0], 0)
                self.assertEqual(ticks[-1], count - 1)
                # 208-unit plot, at least 50 units between five-character date labels.
                self.assertTrue(all((b - a) * 208 / count >= 50 for a, b in zip(ticks, ticks[1:])))

    def test_empty_chart_is_explicit(self):
        self.assertIn("No dated observations", metrics_site.chart([], "Output", [("opened", "Opened")], "count"))


if __name__ == "__main__":
    unittest.main()
