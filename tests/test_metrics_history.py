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
        headline = values["merged_no_rework_share"]
        self.assertEqual((headline["numerator"], headline["denominator"]), (2, 6))
        self.assertEqual(headline["coverage"], {"covered": 6, "total": 6})
        self.assertEqual(headline["status"], "not_tracked")
        self.assertEqual(headline["unclassified_edits"], 1)
        self.assertEqual(values["fixes_pushed_after_review"]["value"], 1)
        self.assertEqual(values["fixes_pushed_after_review"]["total_pushes"], 1)
        self.assertEqual(values["fixes_pushed_before_review"]["value"], 2)
        self.assertEqual(values["review_fix_hours"]["value"], 3)
        self.assertEqual(values["review_fix_hours"]["samples"], 1)
        self.assertNotIn("review_rounds_per_pr", values)
        week = data["weekly"]["rows"][0]
        self.assertEqual((week["cohort_no_rework"], week["cohort_merged"]), (2, 6))
        self.assertEqual((week["history_covered"], week["history_total"]), (7, 8))
        self.assertEqual(week["review_fix_hours"], 3)
        page = metrics_site.render_board(data)
        self.assertIn("2 of 6", page)
        self.assertIn("Too few to show a rate", page)
        self.assertIn("Review history", page)
        self.assertIn("1 commit/force-push updates in total", page)

    def test_review_history_tracked_requires_ninety_percent_coverage(self):
        for count, status in ((8, "partial"), (9, "tracked")):
            row = report(*([record()] * count), *([{"status": "unavailable"}] * (10 - count)))["metrics"]["fixes_pushed_before_review"]
            self.assertEqual(row["status"], status)
            self.assertEqual(row["coverage"], {"covered": count, "total": 10})
            self.assertEqual(row["value"], 0)

    def test_week_uses_github_creation_date_not_merge_date_or_local_submission_guess(self):
        row = record()
        row["created_at"] = "2026-01-04T23:30:00Z"
        data = report(row)
        self.assertEqual(data["weekly"]["rows"][0]["week"], "2025-12-29")
        self.assertEqual(data["weekly"]["rows"][0]["cohort_no_rework"], 1)
        self.assertEqual(data["weekly"]["rows"][0]["opened"], 1)
        self.assertNotIn("first_round_approval_share", data["weekly"]["rows"][0])


class OutcomeTests(unittest.TestCase):
    def outcome(self, row=None, status="merged", outcomes=None, **fields):
        ledger = metrics.normalize_prs([{"url": url(1), "status": status, **fields}])[0][0]
        return history.outcome(history.classify(record() if row is None else row), ledger, outcomes or {})

    def test_merged_conditions_and_primary_reason_precedence(self):
        for row, flags in (
            (record(), []),
            (record(reviews=[review("CHANGES_REQUESTED")]), ["change-requested"]),
            (record(commits=[commit()]), ["author-update"]),
            (record(events=[force()]), ["author-update"]),
            (record(commits=[commit(role="other")]), ["maintainer-repair"]),
            (record(events=[force(role="other")]), ["maintainer-repair"]),
            (record(reviews=[review("CHANGES_REQUESTED")], commits=[commit(), commit(2, role="other")]),
             ["change-requested", "author-update", "maintainer-repair"]),
            (record(commits=[commit(), commit(2, role="other")]), ["author-update", "maintainer-repair"]),
        ):
            with self.subTest(flags=flags):
                result = self.outcome(row)
                self.assertEqual(result["bucket"], "merged_reworked" if flags else "merged_no_rework")
                self.assertEqual(result["flags"], flags)
                self.assertEqual(result["reason"], flags[0] if flags else None)
                self.assertEqual(result["unclassified_edit"], "maintainer-repair" in flags)

    def test_every_maintainer_edit_class_and_evidence(self):
        for kind in ("repair", "scope", "refactor", "style", "tests", "notes", "bot", "base"):
            entry = {"class": kind, "evidence": url(1) + "/commits/" + "a" * 40, "note": "private note"}
            result = self.outcome(record(commits=[commit(role="other")]), outcomes={"schema": 1, "maintainer_edits": {url(1): entry}})
            with self.subTest(kind=kind):
                changed = kind == "repair"
                self.assertEqual(result["bucket"], "merged_reworked" if changed else "merged_no_rework")
                self.assertEqual(result["flags"], ["maintainer-repair"] if changed else [])
                self.assertFalse(result["unclassified_edit"])
                self.assertEqual(result["edit_class"], kind)
                self.assertEqual(result["evidence"], entry["evidence"])
                self.assertNotIn("private note", json.dumps(result))
        result = self.outcome(outcomes={"schema": 1, "maintainer_edits": {url(1): entry}})
        self.assertIsNone(result["evidence"])

    def test_merge_commits_self_reviews_and_post_merge_updates_are_neutral(self):
        row = record(reviews=[review("CHANGES_REQUESTED", role="self")],
                     commits=[commit(merge=True), commit(2, role="other", merge=True),
                              commit(3, at="2026-01-07T00:00:00Z", role="other")],
                     events=[force(at="2026-01-07T00:00:00Z")])
        self.assertEqual(self.outcome(row)["bucket"], "merged_no_rework")

    def test_missing_or_nonmerged_cached_history_cannot_prove_as_submitted(self):
        for row in ({}, {"status": "unavailable"}, record(state="open"), record(state="closed")):
            with self.subTest(row=row):
                result = self.outcome(row)
                self.assertEqual(result["bucket"], "merged_unverified")
                self.assertEqual(result["flags"], [])

    def test_closed_reasons_require_a_valid_classification(self):
        reasons = ("no-real-user", "works-as-designed", "approach-declined", "duplicate", "superseded", "process", "withdrawn", "unexplained")
        for reason in reasons:
            entry = {"reason": reason, "evidence": url(1) + "#issuecomment-1", "note": "closing note"}
            result = self.outcome(status="closed", outcomes={"schema": 1, "closures": {url(1): entry}})
            self.assertEqual((result["bucket"], result["reason"], result["evidence"]), ("closed", reason, entry["evidence"]))
        for entry in ({}, {"reason": "not-a-reason", "evidence": url(1), "note": ""},
                      {"reason": "duplicate", "evidence": "http://github.com/example/project/pull/1", "note": ""}):
            self.assertEqual(self.outcome(status="closed", outcomes={"schema": 1, "closures": {url(1): entry}})["reason"], "unclassified")
        self.assertEqual(self.outcome(status="closed")["reason"], "unclassified")

    def test_invalid_edit_is_unclassified_and_counts_against_the_rate(self):
        result = self.outcome(record(commits=[commit(role="other")]), outcomes={"schema": 1, "maintainer_edits": {
            url(1): {"class": "repair", "evidence": "", "note": ""}}})
        self.assertEqual(result["reason"], "maintainer-repair")
        self.assertTrue(result["unclassified_edit"])
        self.assertIsNone(result["evidence"])

    def test_open_substates_use_saved_human_response_evidence(self):
        cases = [("awaiting-cla", None, "agreement"), ("silent", None, "no-reply"),
                 ("submitted", None, "unchecked"), ("open", {}, "unchecked"),
                 ("in-review", None, "in-review"), ("community-review", {}, "in-review"),
                 ("submitted", {"community": True}, "in-review"),
                 ("submitted", {"maintainer": False, "community": False, "bot": True}, "no-reply"),
                 ("open", {"maintainer": False}, "unchecked"),
                 ("community-review", {"maintainer": False, "community": True}, "in-review"),
                 ("in-review", {"maintainer": True, "community": False}, "in-review"),
                 ("ci-failing", {"maintainer": False, "community": False}, "no-reply"),
                 ("changes-requested", {"maintainer": False, "community": False}, "no-reply"),
                 ("awaiting-cla", {"maintainer": False, "community": False}, "agreement"),
                 ("silent", {"maintainer": True, "community": False}, "in-review")]
        for status, responded, reason in cases:
            with self.subTest(status=status, responded=responded):
                result = self.outcome(record(state="open"), status=status, responded=responded)
                self.assertEqual((result["bucket"], result["reason"], result["flags"]), ("open", reason, []))


class ActivityAcquisitionTests(unittest.TestCase):
    def search_row(self, n=1, **fields):
        return {"html_url": url(n), "state": "closed", "created_at": CREATED, "closed_at": MERGED,
                "updated_at": MERGED, "pull_request": {"merged_at": MERGED}, **fields}

    def test_projection_retains_current_state_event_times_and_reopens_only(self):
        timeline = [{"event": "closed", "created_at": REVIEW, "actor": {"login": "private-user"}},
                    {"event": "reopened", "created_at": FIX}, {"event": "commented", "body": "private prose"}]
        row = history.project_activity(self.search_row(), timeline)
        self.assertEqual(row["status"], "merged")
        self.assertEqual(row["created_at"], "2026-01-05T00:00:00+00:00")
        self.assertEqual([r["event"] for r in row["events"]], ["closed", "reopened"])
        self.assertNotIn("private", json.dumps(row))
        full = history.project({"user": {"login": "author"}, "created_at": CREATED, "merged_at": None, "state": "open"}, [],
                               [{"event": "reopened", "created_at": FIX, "actor": {"login": "author"}}], [], lambda sha: None)
        self.assertEqual(full["events"][0]["event"], "reopened")
        self.assertIsNotNone(history.classify(full))

    def test_reconcile_saves_complete_activity_only_after_every_timeline(self):
        rows = [{"url": url(n), "submitted_at": CREATED} for n in (1, 2)]
        found = [self.search_row(n) for n in (1, 2)]
        with patch.dict(history.CONFIG, {"github_user": "example"}), patch.object(history, "api", side_effect=[found, [], []]) as api:
            check = history.reconcile(rows, include_activity=True)
        self.assertEqual(len(check["activity"]["prs"]), 2)
        self.assertEqual(check["checked_at"], check["activity"]["checked_at"])
        self.assertEqual(api.call_count, 3)
        for call in api.call_args_list[1:]:
            self.assertTrue(call.kwargs["paginate"])
            self.assertIn("/timeline?per_page=100", call.args[0])
        with patch.object(history, "api", return_value=found) as api:
            updated = history.reconcile(rows, previous=check, include_activity=True)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(updated["activity"]["prs"], check["activity"]["prs"])

    def test_public_search_does_not_admit_unledgered_activity(self):
        rows = [{"url": url(1), "submitted_at": CREATED}]
        with patch.object(history, "api", side_effect=[[self.search_row(), self.search_row(2)], [], []]) as api:
            check = history.reconcile(rows, include_activity=True)
        self.assertIn("+is:public+", api.call_args_list[0].args[0])
        self.assertEqual(check["github_only"], 1)
        self.assertEqual([r["url"] for r in check["activity"]["prs"]], [url(1)])
        self.assertEqual(api.call_count, 2)
        result = metrics.activity_projection(rows, metrics.timestamp(MERGED), observation=check["activity"])
        self.assertEqual(result["totals"]["opened"], 1)
        self.assertEqual(result["totals"]["merged"], 1)

    def test_creation_before_submission_midnight_is_included(self):
        rows = [{"url": url(1), "submitted_at": "2026-01-06T00:00:01Z"}]
        found = self.search_row(created_at="2026-01-05T23:59:59Z")
        def api(endpoint, **options):
            if not endpoint.startswith("search/"):
                return []
            cutoff = endpoint.split("created:>=", 1)[1].split("&")[0]
            return [found] if found["created_at"][:10] >= cutoff else []
        with patch.object(history, "api", side_effect=api):
            check = history.reconcile(rows, include_activity=True)
        self.assertNotIn("error", check)
        self.assertEqual(check["activity"]["prs"][0]["created_at"], "2026-01-05T23:59:59+00:00")

    def test_creation_boundary_uses_saved_creation_instead_of_late_submission(self):
        rows = [{"url": url(1), "submitted_at": "2026-01-10T00:00:00Z"}]
        previous = {"activity": {"checked_at": MERGED, "prs": [history.project_activity(self.search_row(), [])]}}
        with patch.object(history, "api", return_value=[self.search_row()]) as api:
            check = history.reconcile(rows, previous=previous, include_activity=True)
        self.assertNotIn("error", check)
        self.assertIn("created:>=2026-01-05&", api.call_args.args[0])
        api.assert_called_once()

    def test_search_missing_ledger_row_is_resolved_only_as_public_upstream(self):
        rows = [{"url": url(1), "submitted_at": "2026-01-10T00:00:00Z"}]
        pr = {**self.search_row(), "merged_at": MERGED, "base": {"repo": {"private": False}}, "user": {"login": "contributor"}}
        with patch.dict(history.CONFIG, {"github_user": "contributor"}), patch.object(history, "api", side_effect=[[], pr, []]) as api:
            check = history.reconcile(rows, include_activity=True)
        self.assertNotIn("error", check)
        self.assertEqual(check["ledger_only"], 1)
        self.assertEqual(api.call_args_list[1].args[0], "repos/example/project/pulls/1")
        self.assertEqual(check["activity"]["prs"][0]["created_at"], "2026-01-05T00:00:00+00:00")
        for fields in ({"base": {"repo": {"private": True}}}, {"base": {}}, {"user": {"login": "someone-else"}}, {"html_url": url(2)}):
            with self.subTest(fields=fields), patch.dict(history.CONFIG, {"github_user": "contributor"}), patch.object(
                    history, "api", side_effect=[[], {**pr, **fields}]):
                failed = history.reconcile(rows, previous=check, include_activity=True)
            self.assertEqual(failed["error"], "incomplete_population")
            self.assertEqual(failed["activity"]["prs"], check["activity"]["prs"])
            self.assertEqual(failed["activity"]["checked_at"], check["activity"]["checked_at"])

    def test_invalid_previous_activity_is_refetched_without_reusing_its_timeline(self):
        previous = {"activity": {"checked_at": MERGED, "prs": [{}]}}
        with patch.object(history, "api", side_effect=[[self.search_row()], []]) as api:
            check = history.reconcile([{"url": url(1), "submitted_at": CREATED}], previous=previous, include_activity=True)
        self.assertNotIn("error", check)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(check["activity"]["ledger_urls"], [url(1)])

    def test_activity_snapshot_keeps_exact_submission_fallback_across_failure(self):
        rows = [{"url": url(1).replace("example/project", "EXAMPLE/PROJECT") + "/", "submitted_at": CREATED}]
        with patch.object(history, "api", side_effect=[[self.search_row(created_at=None)], []]):
            check = history.reconcile(rows, include_activity=True)
        check["activity"]["checked_at"] = MERGED
        good = metrics.activity_projection(rows, metrics.timestamp(MERGED), observation=check["activity"])
        self.assertEqual(good["missing"]["opened_at"], 0)
        self.assertEqual(sum(r["opened"] for r in good["days"]), 1)
        with patch.object(history, "api", side_effect=history.HistoryError("request_failed")):
            failed = history.reconcile(rows, previous=check, include_activity=True)
        changed = [{**rows[0], "submitted_at": "2026-02-01T00:00:00Z"}]
        stale = metrics.activity_projection(changed, metrics.timestamp(MERGED), observation=failed["activity"])
        self.assertEqual(stale["days"], good["days"])
        self.assertEqual(stale["missing"], good["missing"])
        self.assertEqual(stale["as_of"], good["as_of"])
        self.assertFalse(stale["observation"]["ok"])

    def test_changed_metadata_refreshes_reopen_history(self):
        prior = {"checked_at": MERGED, "prs": [history.project_activity(self.search_row(), [])]}
        found = [self.search_row(state="open", closed_at=None, updated_at="2026-01-07T00:00:00Z", pull_request={"merged_at": None})]
        timeline = [{"event": "reopened", "created_at": "2026-01-07T00:00:00Z"}]
        with patch.object(history, "api", return_value=timeline) as api:
            updated = history.activity_observation(found, prior)
        api.assert_called_once()
        self.assertEqual(updated["prs"][0]["status"], "open")
        self.assertEqual(updated["prs"][0]["events"][0]["event"], "reopened")
        self.assertEqual(prior["prs"][0]["status"], "merged")

    def test_api_and_pagination_failure_keep_last_good_observation(self):
        previous = {"activity": {"checked_at": MERGED, "prs": [history.project_activity(self.search_row(), [])]}}
        before = json.dumps(previous, sort_keys=True)
        rows = [{"url": url(1), "submitted_at": CREATED}]
        for error in ("pagination_overflow", "request_failed", "rate_limited_or_forbidden", "transport_error"):
            with patch.object(history, "api", side_effect=history.HistoryError(error)):
                result = history.reconcile(rows, previous=previous, include_activity=True)
            self.assertEqual(result["activity"], {**previous["activity"], "error": error})
            self.assertEqual(json.dumps(previous, sort_keys=True), before)
        with patch.object(history, "api", side_effect=[[self.search_row(1, updated_at="2026-01-07T00:00:00Z"), self.search_row(2)], [], history.HistoryError("request_failed")]):
            failed = history.reconcile(rows + [{"url": url(2), "submitted_at": CREATED}], previous=previous, include_activity=True)
        self.assertEqual(failed["error"], "request_failed")
        self.assertEqual(failed["activity"]["prs"], previous["activity"]["prs"])
        self.assertEqual(failed["activity"]["checked_at"], MERGED)

    def test_ledger_only_rows_do_not_disappear_from_activity_after_a_search(self):
        previous = {"activity": {"checked_at": MERGED, "prs": [history.project_activity(self.search_row(), [])]}}
        with patch.object(history, "api", return_value=[]):
            failed = history.reconcile([{"url": url(1), "submitted_at": CREATED}], previous=previous, include_activity=True)
        self.assertEqual(failed["error"], "incomplete_population")
        self.assertEqual(failed["activity"]["prs"], previous["activity"]["prs"])

    def test_missing_terminal_dates_are_explicit_and_invalid_shapes_fail(self):
        result = history.project_activity(self.search_row(created_at=None, closed_at=None, pull_request={"merged_at": None}), [])
        self.assertEqual(result["status"], "closed")
        self.assertIsNone(result["closed_at"])
        self.assertIsNone(result["created_at"])
        for row in ({}, self.search_row(pull_request={}), self.search_row(state="bad")):
            with self.assertRaises(history.HistoryError):
                history.project_activity(row, [])

    def test_replacement_schema_keeps_old_classifications_and_drops_private_notes(self):
        pair = {"old": url(1), "new": url(2), "source": "private-person@example.test"}
        result, invalid = history.normalize_outcomes({"schema": 2, "replacements": [pair]})
        self.assertEqual(invalid, 0)
        self.assertEqual(result["replacements"][0]["old"], url(1))
        self.assertNotIn("private-person", json.dumps(result))
        for pairs in ("bad", [None], [{**pair, "old": url(2)}], [{**pair, "new": "https://github.com/example/other/pull/2"}]):
            self.assertEqual(history.normalize_outcomes({"schema": 2, "replacements": pairs})[1], 1)
        self.assertEqual(history.normalize_outcomes({"schema": 1})[1], 0)


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

    def test_search_pagination_unwraps_all_items_and_checks_reported_total(self):
        pages = [{"items": [{"html_url": url(n)}], "total_count": 2, "incomplete_results": False} for n in (1, 2)]
        result = SimpleNamespace(returncode=0, stdout=json.dumps(pages), stderr="")
        with patch.object(history.subprocess, "run", return_value=result) as run:
            self.assertEqual(history.api("search/issues?q=type:pr", paginate=True), [{"html_url": url(1)}, {"html_url": url(2)}])
        self.assertIn("--paginate", run.call_args.args[0])
        self.assertIn("--slurp", run.call_args.args[0])
        invalid = [[{"items": [], "incomplete_results": True}], [{"items": "bad"}], [pages[0]],
                   [pages[0], pages[0]], [pages[0], {**pages[1], "total_count": 3}], []]
        for pages in invalid:
            result.stdout = json.dumps(pages)
            with self.subTest(pages=pages), patch.object(history.subprocess, "run", return_value=result), self.assertRaisesRegex(history.HistoryError, "invalid_response"):
                history.api("search/issues?q=type:pr", paginate=True)

    def test_search_overflow_is_not_silently_truncated(self):
        result = SimpleNamespace(returncode=0, stdout=json.dumps([{"items": [], "total_count": 1001, "incomplete_results": False}]), stderr="")
        with patch.object(history.subprocess, "run", return_value=result), self.assertRaisesRegex(history.HistoryError, "pagination_overflow"):
            history.api("search/issues?q=type:pr", paginate=True)

    def test_reconciliation_compares_canonical_urls_and_stores_only_counts(self):
        rows = [{"url": "https://github.com/facebook/lexical/pull/1", "submitted_at": CREATED},
                {"url": url(2), "submitted_at": FIX}]
        with patch.dict(history.CONFIG, {"github_user": "example-user", "repo_renames": {"facebook/lexical": "meta/lexical"}}), patch.object(
                history, "api", return_value=[{"html_url": "https://github.com/meta/lexical/pull/1", "user": {"login": "private"}},
                                             {"html_url": url(3)}]) as api:
            result = history.reconcile(rows)
        self.assertEqual({k: v for k, v in result.items() if k != "checked_at"}, {"github": 2, "ledger": 2, "github_only": 1, "ledger_only": 1})
        self.assertNotIn("private", json.dumps(result))
        api.assert_called_once_with("search/issues?q=author:example-user+type:pr+is:public+-user:example-user+created:>=2026-01-04&per_page=100", paginate=True)

    def test_renamed_repository_reconciles_without_differences_in_either_direction(self):
        old = "https://github.com/Facebook/Lexical/pull/9464/"
        current = "https://github.com/meta/lexical/pull/9464"
        for ledger_url, github_url in ((old, current), (current, old)):
            with self.subTest(ledger_url=ledger_url), patch.dict(history.CONFIG, {
                    "github_user": "example-user", "repo_renames": {"facebook/lexical": "meta/lexical"}}), patch.object(
                    history, "api", return_value=[{"html_url": github_url}]):
                result = history.reconcile([{"url": ledger_url, "submitted_at": CREATED}])
            self.assertEqual({k: v for k, v in result.items() if k != "checked_at"},
                             {"github": 1, "ledger": 1, "github_only": 0, "ledger_only": 0})

    def test_cli_limit_defers_activity_without_extra_requests_or_overwriting_snapshot(self):
        ledger = self.root / "ledger/prs.json"
        write_json(ledger, [{**r, "submitted_at": CREATED} for r in self.prs])
        previous = {"checked_at": MERGED, "activity": {"checked_at": MERGED, "prs": []}}
        write_json(self.path, {**cache(), "reconciliation": previous})
        with patch.object(history, "ROOT", self.root), patch.object(history, "fetch", return_value=record()) as fetch, patch.object(
                history, "api", return_value=[]) as api, redirect_stdout(io.StringIO()) as output:
            result = cli.main(["metrics", "history", "--ledger", str(ledger), "--limit", "1"])
        self.assertEqual(result, 0)
        self.assertEqual(fetch.call_count, 1)
        api.assert_not_called()
        self.assertEqual(history.load_cache(self.path)["reconciliation"], previous)
        self.assertEqual(json.loads(output.getvalue())["reconciliation"], {"status": "deferred", "reason": "limited_refresh"})

    def test_cli_reconciliation_failure_is_nonzero_and_can_be_explicitly_skipped(self):
        ledger = self.root / "ledger/prs.json"
        write_json(ledger, [{**self.prs[0], "submitted_at": CREATED}])
        with patch.object(history, "ROOT", self.root), patch.object(history, "fetch", return_value=record()), patch.object(
                history, "api", side_effect=history.HistoryError("rate_limited_or_forbidden")), redirect_stdout(io.StringIO()):
            result = cli.main(["metrics", "history", "--ledger", str(ledger)])
        self.assertEqual(result, 3)
        check = history.load_cache(self.path)["reconciliation"]
        self.assertEqual(check["error"], "rate_limited_or_forbidden")
        self.assertEqual(set(check), {"checked_at", "error", "activity"})
        self.assertEqual(check["activity"], {"error": "rate_limited_or_forbidden"})
        with patch.object(history, "ROOT", self.root), patch.object(history, "api") as api, redirect_stdout(io.StringIO()):
            result = cli.main(["metrics", "history", "--ledger", str(ledger), "--no-reconcile"])
        self.assertEqual(result, 0)
        api.assert_not_called()
        self.assertEqual(history.load_cache(self.path)["reconciliation"], check)

    def test_reconciliation_missing_date_or_invalid_search_is_content_free(self):
        result = history.reconcile(self.prs)
        self.assertEqual(result["error"], "missing_submission_date")
        for response in ({"items": []}, [{"html_url": "not-a-pr"}]):
            with patch.object(history, "api", return_value=response):
                result = history.reconcile([{**self.prs[0], "submitted_at": CREATED}])
            self.assertEqual(result["error"], "invalid_response")

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
            result = cli.main(["metrics", "history", "--records-root", str(foreign), "--refresh-open", "--limit", "1", "--no-reconcile"])
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
