"""Synthetic upstream histories; no network or private fixtures."""
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from support import DEFAULTS, setup_module
from factory import cli
from factory.core import metrics, metrics_history as history, metrics_site
from factory.util import CommandError, write_json


def setUpModule():
    setup_module(unittest)


CREATED = "2026-01-05T00:00:00Z"
REVIEW = "2026-01-05T02:00:00Z"
FIX = "2026-01-05T05:00:00Z"
MERGED = "2026-01-06T00:00:00Z"


def url(n):
    return f"https://github.com/example/project/pull/{n}"


def review(state="APPROVED", at=REVIEW, role="other"):
    return {"state": state, "submitted_at": at, "role": role}


def commit(n=1, at=FIX, role="self", merge=False):
    return {"sha": f"{n:040x}", "author_at": at, "committed_at": at, "role": role, "merge": merge}


def force(at=FIX, role="self"):
    return {"event": "head_ref_force_pushed", "created_at": at, "role": role}


def record(*, state="merged", reviews=None, commits=None, events=None):
    return {"status": "available", "created_at": CREATED, "merged_at": MERGED if state == "merged" else None,
            "closed_at": MERGED if state != "open" else None, "state": state,
            "reviews": reviews or [], "commits": commits or [], "events": events or []}


def cache(*rows):
    return {"schema_version": history.SCHEMA_VERSION, "prs": {url(n): row for n, row in enumerate(rows, 1)}}


def report(*rows):
    return metrics.compute({"prs": [{"url": url(n), "status": r.get("state", "open"), "submitted_at": CREATED}
                                    for n, r in enumerate(rows, 1)], "pr_history": cache(*rows)}, DEFAULTS)


class ClassificationTests(unittest.TestCase):
    def test_clean_merge_needs_no_review_and_preexisting_commits_are_not_fixes(self):
        result = history.classify(record(commits=[commit(at="2026-01-04T23:00:00Z")]))
        self.assertTrue(result["reviewed"])
        self.assertTrue(result["clean"])
        self.assertEqual(result["rounds"], 1)

    def test_changes_requested_survives_later_approval_and_pairs_next_fix(self):
        result = history.classify(record(reviews=[review("CHANGES_REQUESTED"), review(at="2026-01-05T08:00:00Z")],
                                         commits=[commit(), commit(2, "2026-01-05T07:00:00Z")]))
        self.assertFalse(result["clean"])
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(result["after_review_pushes"], 2)
        self.assertEqual(result["fix_hours"], [3])

    def test_pre_review_self_correction_is_a_defect_even_if_it_merges(self):
        result = history.classify(record(commits=[commit()], reviews=[review(at="2026-01-05T08:00:00Z")]))
        self.assertFalse(result["clean"])
        self.assertEqual(result["before_review_pushes"], 1)
        self.assertEqual(result["after_review_pushes"], 0)

    def test_merge_base_only_update_is_not_a_fix(self):
        result = history.classify(record(reviews=[review()], commits=[commit(merge=True), commit(2, role="other", merge=True)]))
        self.assertTrue(result["clean"])
        self.assertEqual(result["after_review_pushes"], 0)

    def test_maintainer_fix_before_merge_is_not_first_round_clean(self):
        result = history.classify(record(commits=[commit(role="other")]))
        self.assertFalse(result["clean"])
        self.assertEqual(result["other_pushes"], 1)
        self.assertEqual(result["self_pushes"], 0)

    def test_force_push_counts_and_exact_duplicate_event_does_not(self):
        result = history.classify(record(reviews=[review("CHANGES_REQUESTED")], events=[force(), force()]))
        self.assertFalse(result["clean"])
        self.assertEqual(result["after_review_pushes"], 1)
        self.assertEqual(result["fix_hours"], [3])
        result = history.classify(record(commits=[commit(), commit()], events=[force()]))
        self.assertEqual(result["self_pushes"], 1)

    def test_unreviewed_open_and_self_reviews_are_excluded_but_comments_are_reviewed(self):
        for reviews in ([], [review(role="self")]):
            result = history.classify(record(state="open", reviews=reviews))
            self.assertFalse(result["reviewed"])
            self.assertFalse(result["clean"])
        result = history.classify(record(state="open", reviews=[review("COMMENTED")]))
        self.assertTrue(result["reviewed"])
        self.assertFalse(result["clean"])
        self.assertTrue(history.classify(record(state="open", reviews=[review()]))["clean"])

    def test_comment_only_review_does_not_establish_request_to_fix_duration(self):
        result = history.classify(record(reviews=[review("COMMENTED")], commits=[commit()]))
        self.assertEqual(result["after_review_pushes"], 1)
        self.assertEqual(result["fix_hours"], [])

    def test_committer_date_catches_reauthored_old_commit_and_ignores_post_merge_activity(self):
        changed = commit()
        changed["author_at"] = "2026-01-01T00:00:00Z"
        result = history.classify(record(commits=[changed, commit(2, at="2026-01-07T00:00:00Z")],
                                         reviews=[review("CHANGES_REQUESTED", at="2026-01-07T00:00:00Z")]))
        self.assertFalse(result["clean"])
        self.assertEqual(result["self_pushes"], 1)
        self.assertEqual(result["changes_requested"], 0)

    def test_unavailable_missing_invalid_and_failed_refresh_are_not_clean(self):
        bad = [None, {}, {"status": "unavailable"}, {**record(), "refresh_error": "request_failed"},
               {**record(), "commits": None}, {**record(), "created_at": "2026-01-05"},
               record(commits=[{**commit(), "role": None}]), record(reviews=[review(at="bad")])]
        for row in bad:
            with self.subTest(row=row):
                self.assertIsNone(history.classify(row))

    def test_aggregate_cohort_coverage_and_weekly_submission_series(self):
        rows = [record(), record(reviews=[review("CHANGES_REQUESTED")], commits=[commit()]),
                record(commits=[commit()]), record(commits=[commit(merge=True)]),
                record(commits=[commit(role="other")]), record(events=[force()]),
                {"status": "unavailable"}, record(state="open")]
        data = report(*rows)
        values = data["metrics"]
        headline = values["first_round_approval_share"]
        self.assertEqual((headline["numerator"], headline["denominator"]), (2, 6))
        self.assertEqual(headline["coverage"], {"covered": 7, "total": 8})
        self.assertEqual(headline["status"], "partial")
        self.assertEqual(headline["unreviewed"], 1)
        self.assertEqual(values["fixes_pushed_after_review"]["value"], 1)
        self.assertEqual(values["fixes_pushed_after_review"]["total_pushes"], 1)
        self.assertEqual(values["fixes_pushed_before_review"]["value"], 2)
        self.assertEqual(values["review_fix_hours"]["value"], 3)
        self.assertEqual(values["review_fix_hours"]["samples"], 1)
        self.assertEqual(values["review_rounds_per_pr"]["value"], 1.167)
        week = data["weekly"]["rows"][0]
        self.assertEqual((week["first_round_numerator"], week["first_round_denominator"]), (2, 6))
        self.assertEqual((week["history_covered"], week["history_total"]), (7, 8))
        self.assertEqual(week["review_fix_hours"], 3)
        page = metrics_site.render(data)
        self.assertIn("2/6 reviewed PRs clean", page)
        self.assertIn("First-round quality", page)
        self.assertIn("1 commit/force-push updates in total", page)

    def test_tracked_requires_ninety_percent_coverage(self):
        headline = report(*([record()] * 9), {"status": "unavailable"})["metrics"]["first_round_approval_share"]
        self.assertEqual(headline["status"], "tracked")
        self.assertEqual(headline["denominator"], 9)
        self.assertEqual(headline["value"], 1)

    def test_week_uses_github_creation_date_not_merge_date_or_local_submission_guess(self):
        row = record()
        row["created_at"] = "2026-01-04T23:30:00Z"
        data = report(row)
        self.assertEqual(data["weekly"]["rows"][0]["week"], "2025-12-29")
        self.assertEqual(data["weekly"]["rows"][0]["first_round_denominator"], 1)
        self.assertIsNone(data["weekly"]["rows"][1]["first_round_approval_share"])


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.path = self.root / "ledger/pr-history.json"
        self.path.parent.mkdir()
        self.prs = [{"url": url(n), "status": "open"} for n in range(1, 4)]

    def test_api_always_uses_factory_wrapper_get_pagination_and_timeout(self):
        result = SimpleNamespace(returncode=0, stdout='[[{"state":"APPROVED"}],[]]', stderr="")
        with patch.object(history.subprocess, "run", return_value=result) as run:
            self.assertEqual(history.api("repos/example/project/pulls/1/reviews?per_page=100", paginate=True), [{"state": "APPROVED"}])
        args = run.call_args.args[0]
        self.assertEqual(args[:3], [str(history.ROOT / "bin/factory"), "gh", "api"])
        self.assertEqual(args[-4:], ["--method", "GET", "--paginate", "--slurp"])
        self.assertEqual(run.call_args.kwargs["timeout"], 120)

    def test_api_errors_are_content_free_and_paginated_shapes_are_validated(self):
        for stderr, expected in (("HTTP 404 private text", "unavailable"), ("HTTP 429 private text", "rate_limited_or_forbidden"),
                                 ("rate limit exceeded", "rate_limited_or_forbidden"), ("refused private text", "request_failed")):
            with patch.object(history.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr=stderr)):
                with self.assertRaisesRegex(history.HistoryError, "^" + expected + "$"):
                    history.api("repos/example/project/pulls/1")
        with patch.object(history.subprocess, "run", side_effect=subprocess.TimeoutExpired("factory", 120)):
            with self.assertRaisesRegex(history.HistoryError, "transport_error"):
                history.api("repos/example/project/pulls/1")
        with patch.object(history.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout='[{"private":"text"}]', stderr="")):
            with self.assertRaisesRegex(history.HistoryError, "invalid_response"):
                history.api("repos/example/project/pulls/1", paginate=True)

    def test_concluded_rows_are_reused_open_rows_refresh_and_force_reloads(self):
        write_json(self.path, cache(record(), record(state="closed"), record(state="open")))
        with patch.object(history, "fetch", return_value=record()) as fetch:
            result = history.refresh(self.prs, self.path)
            fetch.assert_called_once_with(url(3))
            self.assertEqual(result["skipped"], 2)
        with patch.object(history, "fetch", return_value=record()) as fetch:
            history.refresh(self.prs, self.path, force=True, limit=2)
            self.assertEqual(fetch.call_count, 2)

    def test_refresh_preserves_previously_observed_commits_removed_by_rebase(self):
        old = record(state="open", commits=[commit()])
        write_json(self.path, cache(old))
        with patch.object(history, "fetch", return_value=record()):
            history.refresh(self.prs[:1], self.path)
        row = history.load_cache(self.path)["prs"][url(1)]
        self.assertEqual(row["state"], "merged")
        self.assertFalse(history.classify(row)["clean"])
        self.assertEqual(history.classify(row)["self_pushes"], 1)

    def test_history_retention_keeps_same_second_reviews_without_duplicating_refetches(self):
        old = record(reviews=[review("CHANGES_REQUESTED")] * 2)
        retained = history.retain_observed(old, record(reviews=[review("CHANGES_REQUESTED")] * 2))
        self.assertEqual(history.classify(retained)["rounds"], 3)

    def test_failed_refresh_excludes_prior_clean_open_snapshot(self):
        write_json(self.path, cache(record(state="open", reviews=[review()])))
        with patch.object(history, "fetch", side_effect=history.HistoryError("rate_limited_or_forbidden")):
            history.refresh(self.prs[:1], self.path)
        row = history.load_cache(self.path)["prs"][url(1)]
        self.assertEqual(len(row["reviews"]), 1)
        self.assertIsNone(history.classify(row))

    def test_404_is_cached_without_stopping_other_prs(self):
        with patch.object(history, "fetch", side_effect=[history.HistoryError("unavailable"), record(), record()]):
            result = history.refresh(self.prs, self.path)
        saved = history.load_cache(self.path)
        self.assertEqual(saved["prs"][url(1)]["status"], "unavailable")
        self.assertEqual(result["unavailable"], 1)
        self.assertEqual(result["refreshed"], 3)
        self.assertIsNone(result["stopped"])

    def test_rate_limit_stops_checkpoints_and_never_uses_old_clean_snapshot(self):
        old = record(state="open", reviews=[review()])
        write_json(self.path, cache(old))
        with patch.object(history, "fetch", side_effect=[record(), record(), history.HistoryError("rate_limited_or_forbidden")]) as fetch:
            result = history.refresh(self.prs, self.path)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(result["stopped"], "rate_limited_or_forbidden")
        saved = history.load_cache(self.path)["prs"]
        self.assertIsNone(history.classify(saved[url(3)]))
        self.assertEqual(saved[url(1)]["state"], "merged")

    def test_corrupt_cache_is_not_overwritten(self):
        self.path.write_text("broken")
        with self.assertRaises(CommandError), patch.object(history, "fetch") as fetch:
            history.refresh(self.prs, self.path)
        fetch.assert_not_called()
        self.assertEqual(self.path.read_text(), "broken")

    def test_cli_foreign_root_reads_only_ledger_and_cache_stays_in_own_ledger(self):
        foreign = self.root / "records"
        (foreign / "ledger").mkdir(parents=True)
        write_json(foreign / "ledger/prs.json", self.prs)
        with patch.object(history, "ROOT", self.root), patch.object(history, "fetch", return_value=record()), redirect_stdout(io.StringIO()):
            result = cli.main(["metrics", "history", "--records-root", str(foreign), "--refresh-open", "--limit", "1"])
        self.assertEqual(result, 0)
        self.assertTrue(self.path.exists())
        self.assertFalse((foreign / "ledger/pr-history.json").exists())
        with patch.object(history, "ROOT", self.root), redirect_stdout(io.StringIO()):
            result = cli.main(["metrics", "history", "--ledger", str(foreign / "ledger/prs.json"), "--cache", str(foreign / "ledger/pr-history.json")])
        self.assertEqual(result, 3)

    def test_projection_retains_only_structural_evidence_and_recovers_dismissed_requests(self):
        pr = {"created_at": CREATED, "closed_at": MERGED, "merged_at": MERGED, "state": "closed",
              "user": {"login": "contributor"}, "body": "private body"}
        raw_commit = {"sha": "a" * 40, "author": {"login": "contributor"}, "committer": {"login": "web-flow"},
                      "parents": [{"sha": "b" * 40}], "commit": {"message": "private subject", "author": {"date": FIX, "email": "private"},
                                                                   "committer": {"date": FIX, "email": "private"}}}
        reviews = [{"id": 1, "state": "DISMISSED", "submitted_at": REVIEW, "user": {"login": "reviewer"}, "body": "private review"}]
        events = [{"event": "committed", "sha": "a" * 40},
                  {"event": "review_dismissed", "dismissed_review": {"review_id": 1, "state": "changes_requested"}},
                  {"event": "head_ref_force_pushed", "created_at": FIX, "actor": {"login": "contributor"}}]
        with patch.object(history, "api", return_value=raw_commit) as resolve:
            projected = history.project(pr, reviews, events, [], resolve)
        resolve.assert_called_once_with("a" * 40)
        text = json.dumps(projected)
        for forbidden in ("private", "contributor", "reviewer", "email", "body", "message", "login"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(projected["commits"][0]["role"], "self")
        self.assertEqual(projected["reviews"][0]["state"], "CHANGES_REQUESTED")
        self.assertEqual(history.classify(projected)["fix_hours"], [3])
        raw_commit["committer"] = {"login": "reviewer"}
        self.assertEqual(history.commit_role(raw_commit, "contributor"), "other")

    def test_fetch_paginates_reviews_timeline_and_commits(self):
        with patch.object(history, "api", side_effect=[{}, [], [], []]) as api, patch.object(history, "project", return_value=record()):
            history.fetch(url(1))
        self.assertEqual(api.call_count, 4)
        for call in api.call_args_list[1:]:
            self.assertTrue(call.kwargs["paginate"])
            self.assertIn("per_page=100", call.args[0])


if __name__ == "__main__":
    unittest.main()
