import datetime as dt
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import setup_module

from factory import cli  # noqa: E402
from factory.core import review, verdicts  # noqa: E402
from factory.oss import plan  # noqa: E402
from factory.util import CONFIG, CommandError, GH_READ_TIMEOUT, Item, write_json  # noqa: E402

AT = dt.datetime(2026, 11, 4, 12, tzinfo=dt.timezone.utc)
VERIFY = "verdict: ship\nWho hits it: issue #12, users of the bedrock adapter\nImpact: tool results show up instead of vanishing\n"


def setUpModule():
    setup_module(unittest)


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def ago(days=0, hours=0):
    return (AT - dt.timedelta(days=days, hours=hours)).isoformat()


def took(days, hours):
    """A merge time `hours` after a submission `days` ago."""
    return ago(days, -hours)


def row(repo, number, status, submitted, merged=None):
    data = {"url": f"https://github.com/{repo}/pull/{number}", "repo": repo, "number": number, "status": status, "submitted_at": submitted}
    if merged:
        data["merged_at"] = merged
    return data


class Workspace(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name).resolve()
        self.runs = self.root / "runs"
        self.ledger = self.root / "ledger" / "prs.json"
        self.ledger.parent.mkdir()
        write_json(self.ledger, [])
        for mocker in (patch.dict(CONFIG, {"runs_dir": str(self.runs), "ledger_file": str(self.ledger)}),
                       patch.object(plan, "now", return_value=AT)):
            mocker.start()
            self.addCleanup(mocker.stop)

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, json.loads(out.getvalue())


class Plan(Workspace):
    @classmethod
    def setUpClass(cls):
        """One clone for the class: branch refs are free, commits run the machine's commit hooks."""
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.clone = pathlib.Path(temporary.name).resolve() / "clone"
        sh(temporary.name, "git", "init", "-q", "-b", "main", str(cls.clone))
        sh(cls.clone, "git", "config", "user.name", CONFIG["git_name"])
        sh(cls.clone, "git", "config", "user.email", CONFIG["git_email"])
        shas = []
        for text in ("reviewed", "changed after review"):
            (cls.clone / "a.txt").write_text(text + "\n")
            sh(cls.clone, "git", "add", "a.txt")
            sh(cls.clone, "git", "commit", "-qm", text)
            shas.append(sh(cls.clone, "git", "rev-parse", "HEAD"))
        cls.reviewed, cls.changed = shas

    def branch(self, name, at=None):
        sh(self.clone, "git", "branch", "-f", name, at or self.reviewed)
        return at or self.reviewed

    def item(self, name, status="preparing", review=None, verdict="ship", verify=VERIFY, rounds=0, **meta):
        folder = self.runs / "2026-11-04" / name
        folder.mkdir(parents=True)
        write_json(folder / "item.json", {"line": "oss", "repo": "o/r", "branch": f"contrib/{name}", "clone": str(self.clone),
                                          "implementer": "model-a", "status": status, **meta})
        if review is not None:
            head = f"head: {review}\n" if review else ""
            (folder / "review.md").write_text(f"reviewer: model-b (fresh context)\n{head}verdict: {verdict}\n\nverdict: drop in round one\n")
            (folder / "coordinator-review.md").write_text(f"{head}verdict: ship\n")
        if verify is not None:
            (folder / "verify.md").write_text(verify)
        for n in range(1, rounds + 1):
            (folder / f"review-round{n}.md").write_text(f"head: 0000000\nverdict: fix-needed\nround {n}\n")
        return folder

    def names(self, report, bucket):
        return sorted(pathlib.Path(entry["item"]).name for entry in report[bucket])

    def test_ship_ready_requires_the_review_to_name_the_current_head(self):
        self.item("ship", review=self.branch("contrib/ship"))
        self.branch("contrib/stale", self.changed)
        self.item("stale", review=self.reviewed)
        report = plan.run()
        self.assertEqual(self.names(report, "ship_ready"), ["ship"])
        self.assertEqual(self.names(report, "stale_reviews"), ["stale"])
        self.assertEqual((report["stale_reviews"][0]["reviewed"], report["stale_reviews"][0]["head"]), (self.reviewed, self.changed))
        self.assertEqual(report["ship_ready"][0]["verdict"], "ship")

    def test_ship_ready_requires_who_hits_it_and_impact_lines(self):
        self.item("no-impact", review=self.branch("contrib/no-impact"), verify="verdict: ship\nWho hits it: issue #12 reporters\n")
        self.item("no-verify", review=self.branch("contrib/no-verify"), verify=None)
        report = plan.run()
        self.assertEqual(report["ship_ready"], [])
        held = {pathlib.Path(e["item"]).name: e["reasons"] for e in report["held"]}
        self.assertEqual(held["no-impact"], ["verify.md lacks an impact line"])
        self.assertIn("verify.md lacks a who-hits-it line and an impact line", held["no-verify"])

    def test_review_without_a_head_line_is_held_not_shipped(self):
        self.branch("contrib/unbound")
        self.item("unbound", review="")
        report = plan.run()
        self.assertEqual(report["ship_ready"], [])
        (reason,) = report["held"][0]["reasons"]
        self.assertTrue(reason.startswith("review.md has no head: line, so it is bound to no commit; "), reason)
        item = report["held"][0]["item"]
        self.assertIn(f"bin/factory review stamp --item {item} review --head <head>", reason)
        self.assertIn(f"bin/factory review round --item {item}", reason)

    def test_items_with_a_pr_or_a_terminal_status_stay_out(self):
        for name, status, extra in (("sent", "submitted", {"pr_url": "https://github.com/o/r/pull/9"}),
                                    ("approved-sent", "approved", {"pr_url": "https://github.com/o/r/pull/10"}),
                                    ("dropped", "dropped", {}), ("closed", "closed", {}), ("abandoned", "abandoned", {})):
            self.item(name, status=status, review=self.branch(f"contrib/{name}"), **extra)
        report = plan.run()
        for bucket in ("ship_ready", "needs_coordinator_review", "fix_round", "stale_reviews", "held", "unreviewed", "missing_clone", "errors", "parked_or_blocked"):
            self.assertEqual(report[bucket], [], bucket)

    def test_fix_round_counts_review_rounds_and_points_at_review(self):
        folder = self.item("fix", review=self.branch("contrib/fix"), verdict="fix-needed", rounds=2)
        (folder / "review-round3.json").write_text("{}")
        report = plan.run()
        self.assertEqual(self.names(report, "fix_round"), ["fix"])
        entry = report["fix_round"][0]
        self.assertEqual(entry["review_rounds"], 2)
        self.assertEqual(entry["review"], str(folder / "review.md"))
        self.assertEqual(report["ship_ready"], [])

    def test_missing_clone_is_listed_and_the_rest_still_planned(self):
        self.item("gone", review="abcdef1", clone=str(self.root / "no-such-clone"))
        self.item("ship", review=self.branch("contrib/ship"))
        report = plan.run()
        self.assertEqual(self.names(report, "missing_clone"), ["gone"])
        self.assertEqual(report["missing_clone"][0]["clone"], str(self.root / "no-such-clone"))
        self.assertEqual(self.names(report, "ship_ready"), ["ship"])
        self.assertEqual(report["errors"], [])

    def test_unknown_branch_head_is_an_error_and_fails_the_command(self):
        self.item("lost", review="abcdef1")
        code, report = self.run_cli("plan")
        self.assertEqual(code, 1)
        self.assertEqual(self.names(report, "errors"), ["lost"])
        self.assertIn("branch head unknown", report["errors"][0]["error"])
        self.assertEqual(report["ship_ready"], [])

    def test_parked_and_blocked_items_show_the_first_160_characters_of_their_reason(self):
        self.item("blocked", status="blocked", blocked_reason="x" * 300, pr_url="https://github.com/o/r/pull/3")
        self.item("parked", status="parked", parked_reason="waiting for the release")
        self.item("unreviewed")
        report = plan.run()
        reasons = {pathlib.Path(e["item"]).name: e["reason"] for e in report["parked_or_blocked"]}
        self.assertEqual(reasons, {"blocked": "x" * 160, "parked": "waiting for the release"})
        self.assertEqual(self.names(report, "unreviewed"), ["unreviewed"])
        self.assertEqual(report["missing_clone"] + report["errors"], [])

    def test_repo_slots_report_medians_and_order_without_count_caps(self):
        write_json(self.ledger, [
            row("a/fast", 1, "merged", ago(9), took(9, 2)),
            row("a/fast", 2, "merged", ago(8), took(8, 4)),
            row("a/fast", 3, "submitted", ago(1)),
            *[row("b/many", n, "merged", ago(9), took(9, h)) for n, h in ((1, 10), (2, 30), (3, 20))],
            row("c/full", 1, "merged", ago(9), ago(8)),
            *[row("c/full", n, "in-review", ago(1)) for n in (2, 3, 4)],
            row("d/none", 1, "submitted", ago(1)),
            row("d/none", 2, "closed", ago(5)),
            row("e/slow", 1, "merged", ago(9), took(9, 40)),
            row("e/slow", 2, "merged", ago(9), took(9, 60)),
        ])
        slots = plan.run()["repo_slots"]
        self.assertEqual([s["repo"] for s in slots], ["b/many", "a/fast", "e/slow", "c/full"])
        fast = slots[1]
        self.assertEqual({k: fast[k] for k in ("open", "merged", "median_merge_hours", "oldest_open_days", "stopped", "stop_reasons")},
                         {"open": 1, "merged": 2, "median_merge_hours": 3.0, "oldest_open_days": 1.0, "stopped": False, "stop_reasons": []})
        self.assertEqual((slots[0]["median_merge_hours"], slots[0]["oldest_open_days"]), (20.0, None))
        self.assertEqual(slots[3]["open"], 3)
        self.assertTrue(all(not slot["stopped"] for slot in slots))
        for slot in slots:
            self.assertNotIn("cap", slot)
            self.assertNotIn("free", slot)

    def test_unresponsive_repos_sort_after_responsive_ones_even_with_more_merges(self):
        write_json(self.ledger, [
            row("s/silent", 1, "merged", ago(60), ago(59)),
            row("s/silent", 2, "merged", ago(58), ago(57)),
            *[row("s/silent", n, "silent", ago(40)) for n in (3, 4, 5)],
            row("t/waiting", 1, "merged", ago(20), ago(10)),
            row("t/waiting", 2, "silent", ago(8)),
            row("u/merged-since", 1, "submitted", ago(5)),
            row("u/merged-since", 2, "merged", ago(3), ago(1)),
        ])
        ordered = plan.run()["repo_slots"]
        slots = {s["repo"]: s for s in ordered}
        self.assertTrue(slots["s/silent"]["stopped"])
        self.assertEqual(len(slots["s/silent"]["stop_reasons"]), 1)
        self.assertFalse(slots["t/waiting"]["stopped"])
        self.assertFalse(slots["u/merged-since"]["stopped"])
        self.assertEqual([s["repo"] for s in ordered], ["u/merged-since", "t/waiting", "s/silent"])

    def test_unreadable_ledger_refuses_to_plan(self):
        self.ledger.write_text("{not json")
        with self.assertRaises(SystemExit) as raised:
            plan.run()
        self.assertIn("unreadable", str(raised.exception))


class Unresponsive(Workspace):
    def waiting(self, ages=(40, 35, 30)):
        return [row("o/r", n, "community-review" if n == 2 else "silent", ago(days))
                for n, days in enumerate(ages, 1)]

    def test_two_old_silent_prs_are_not_enough_history(self):
        rows = [row("o/r", n, "silent", ago(40)) for n in (1, 2)]
        self.assertEqual(plan.unresponsive(rows, at=AT), [])

    def test_three_long_unanswered_prs_name_the_waiting_sample(self):
        reason, = plan.unresponsive(self.waiting(), at=AT)
        for phrase in ("3 of our PRs", "#1 (40 days)", "#2 (35 days)", "#3 (30 days)", "30+ days", ago(40)[:10]):
            self.assertIn(phrase, reason)

    def test_a_recent_third_pr_does_not_complete_the_sample(self):
        self.assertEqual(plan.unresponsive(self.waiting((40, 35, 10)), at=AT), [])

    def test_a_maintainer_review_clears_the_pause(self):
        for status in ("in-review", "changes-requested"):
            with self.subTest(status=status):
                self.assertEqual(plan.unresponsive(self.waiting() + [row("o/r", 4, status, ago(20))], at=AT), [])

    def test_answer_times_not_pr_creation_times_decide(self):
        for status in ("in-review", "changes-requested"):
            with self.subTest(status=status):
                old_pr = row("o/r", 4, status, ago(60))
                self.assertTrue(plan.unresponsive(self.waiting() + [{**old_pr, "maintainer_answered_at": ago(45)}], at=AT),
                                "an answer before the oldest waiting PR does not excuse later silence")
                self.assertEqual(plan.unresponsive(self.waiting() + [{**old_pr, "maintainer_answered_at": ago(1)}], at=AT), [],
                                 "yesterday's answer on an old PR shows maintainers respond")
                self.assertEqual(plan.unresponsive(self.waiting() + [old_pr], at=AT), [],
                                 "an answer of unknown time cannot be ruled out, so it counts")

    def test_recorded_maintainer_responses_decide_not_the_status(self):
        answered_ci = {**row("o/r", 4, "ci-failing", ago(20)), "responded": {"maintainer": True}}
        self.assertEqual(plan.unresponsive(self.waiting() + [answered_ci], at=AT), [],
                         "a maintainer answer on a newer PR whose CI fails still counts")
        quiet_ci = {**row("o/r", 4, "ci-failing", ago(20)), "responded": {"maintainer": False}}
        self.assertTrue(plan.unresponsive(self.waiting() + [quiet_ci], at=AT))
        answered_silent = self.waiting()
        answered_silent[0] = {**answered_silent[0], "responded": {"maintainer": True}}
        self.assertEqual(plan.unresponsive(answered_silent, at=AT), [], "an answered PR is not part of the waiting sample")

    def test_merge_since_the_oldest_waiting_pr_clears_the_pause(self):
        for days in (39, 40):
            with self.subTest(days=days):
                merged = row("o/r", 4, "merged", ago(50), ago(days))
                self.assertEqual(plan.unresponsive(self.waiting() + [merged], at=AT), [])

    def test_merge_before_the_oldest_waiting_pr_does_not_clear_the_pause(self):
        merged = row("o/r", 4, "merged", ago(50), ago(41))
        self.assertEqual(plan.unresponsive(self.waiting() + [merged], at=AT), plan.unresponsive(self.waiting(), at=AT))

    def test_close_since_the_oldest_waiting_pr_clears_the_pause(self):
        for days in (39, 40):
            with self.subTest(days=days):
                closed = {**row("o/r", 4, "closed", ago(50)), "closed_at": ago(days)}
                self.assertEqual(plan.unresponsive(self.waiting() + [closed], at=AT), [])

    def test_slot_exposes_response_reasons_without_cap_or_free(self):
        for rows in (self.waiting(), self.waiting((40, 35, 10))):
            with self.subTest(ages=[r["submitted_at"] for r in rows]):
                slot = plan.slot(rows)
                reasons = plan.unresponsive(rows, at=AT)
                self.assertEqual((slot["stopped"], slot["stop_reasons"]), (bool(reasons), reasons))
                self.assertNotIn("cap", slot)
                self.assertNotIn("free", slot)


class SchemaTwoStages(Workspace):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name).resolve()
        cls.clone = root / "clone"
        upstream = root / "upstream.git"
        fork = root / "fork.git"
        for remote in (upstream, fork):
            sh(root, "git", "init", "--bare", "-q", "-b", "main", str(remote))
        sh(root, "git", "clone", "-q", str(upstream), str(cls.clone))
        sh(cls.clone, "git", "config", "user.name", "Contributor")
        sh(cls.clone, "git", "config", "user.email", "contributor@users.noreply.github.com")
        sh(cls.clone, "git", "remote", "add", "fork", str(fork))
        (cls.clone / "input.txt").write_text("base\n")
        sh(cls.clone, "git", "add", "input.txt")
        sh(cls.clone, "git", "commit", "-qm", "Base input")
        sh(cls.clone, "git", "push", "-q", "origin", "main")
        cls.base = sh(cls.clone, "git", "rev-parse", "HEAD")
        sh(cls.clone, "git", "checkout", "-qb", "contrib/fix")
        (cls.clone / "input.txt").write_text("fixed\n")
        sh(cls.clone, "git", "add", "input.txt")
        sh(cls.clone, "git", "commit", "-qm", "Preserve supported input")
        cls.head = sh(cls.clone, "git", "rev-parse", "HEAD")

    def setUp(self):
        super().setUp()
        mocker = patch.dict(CONFIG, {"repos_dir": str(self.root / "repos")})
        mocker.start()
        self.addCleanup(mocker.stop)
        folder = self.runs / "day" / "candidate"
        folder.mkdir(parents=True)
        write_json(folder / "item.json", {"schema": 2, "repo": "o/r", "clone": str(self.clone),
                                          "branch": "contrib/fix", "base": "main", "status": "preparing"})
        self.item = Item(folder)
        self.demand = {"author": "scout", "claim": "Supported input is lost.",
                       "who_hits_it": {"kind": "external-report", "source": "https://github.com/o/r/issues/1",
                                       "quote": "My input was lost.", "self_filed": False},
                       "contract": {}, "workaround": "Preserve the input manually.", "error_path": False,
                       "real_occurrence": "", "decision": "build"}
        self.save_demand()

    def save_demand(self):
        write_json(self.item.file("demand.json"), self.demand)

    def value(self, verdict="build", **fields):
        data = {"reviewer": "independent", "complete": True, "verdict": verdict,
                "demand_sha256": verdicts.demand_sha256(self.item),
                "answers": {key: {"ok": True, "note": "Checked the external report."} for key in review.VALUE_ANSWERS},
                **fields}
        write_json(self.item.file("value-review.json"), data)

    def assert_stage(self, bucket, reasons=None):
        actual, entry = plan.classify(self.item)
        self.assertEqual(actual, bucket, entry)
        if reasons is not None:
            self.assertEqual(entry["reasons"], reasons)
        report = plan.run()
        populated = [key for key, entries in report.items() if isinstance(entries, list) and entries and key != "repo_slots"]
        self.assertEqual(populated, [bucket])
        self.assertEqual(report[bucket][0]["item"], str(self.item.dir))
        for key in ("needs_value_review", "proposal_ready", "needs_implementation"):
            self.assertIn(key, report)
        return entry

    def test_missing_or_invalid_demand_is_held_with_validator_reasons(self):
        self.item.file("demand.json").unlink()
        self.assert_stage("held", review.validate_demand(self.item))
        for text in ("{", "[]", "{}"):
            self.item.file("demand.json").write_text(text)
            self.assert_stage("held", review.validate_demand(self.item))

    def test_scout_drop_has_the_exact_disposition(self):
        self.demand["decision"] = "drop"
        self.save_demand()
        self.assert_stage("held", ["the scout dropped this candidate: set status to dropped in item.json with the reason"])

    def test_scout_proposal_needs_an_independent_value_review_without_a_clone(self):
        self.demand["decision"] = "propose"
        self.save_demand()
        self.item.file("proposal.md").write_text("Plan, scope, test plan and a question.\n")
        self.item.meta["clone"] = str(self.root / "absent")
        self.item.save()
        self.assert_stage("needs_value_review")
        self.value("propose")
        self.assert_stage("proposal_ready")

    def test_missing_value_review_resumes_after_build_scout(self):
        self.assert_stage("needs_value_review")

    def test_invalid_or_stale_value_review_is_held_with_validator_reasons(self):
        for text in ("{", "[]", "{}"):
            self.item.file("value-review.json").write_text(text)
            self.assert_stage("held", review.validate_value(self.item))
        self.value(demand_sha256="outdated")
        self.assert_stage("held", review.validate_value(self.item))

    def test_explicitly_incomplete_value_review_never_advances(self):
        for verdict in ("build", "propose", "drop"):
            self.value(verdict, complete=False)
            entry = self.assert_stage("held")
            self.assertIn("complete", " ".join(entry["reasons"]))
        self.value(complete="true")
        entry = self.assert_stage("held")
        self.assertIn("complete", " ".join(entry["reasons"]))

    def test_value_drop_has_the_exact_disposition(self):
        self.value("drop")
        self.assert_stage("held", ["the value review says drop: set status to dropped in item.json and copy its reason"])

    def test_proposal_needs_a_nonempty_draft(self):
        self.value("propose")
        reason = ["write proposal.md as the value-review card describes"]
        self.assert_stage("held", reason)
        self.item.file("proposal.md").write_text(" \n\t")
        self.assert_stage("held", reason)
        self.item.file("proposal.md").write_text("Should the supported contract include this input?\n")
        self.assert_stage("proposal_ready")

    def test_build_resumes_until_both_a_change_and_verify_exist(self):
        self.value()
        self.assert_stage("needs_implementation")
        self.item.file("verify.md").write_text(VERIFY)
        self.item.meta["branch"] = "main"
        self.item.save()
        self.assert_stage("needs_implementation")
        self.item.meta["branch"] = "contrib/fix"
        self.item.save()
        self.assert_stage("unreviewed")

    def test_committed_build_waits_for_a_ship_self_review(self):
        self.value()
        template = (ROOT / "lines/oss/templates/verify.md").read_text()
        for text in (template, "", "verdict: fix-needed\n", "verdict: drop\n"):
            with self.subTest(verify=text):
                self.item.file("verify.md").write_text(text)
                self.assertNotEqual(self.item.head_sha(), self.item.merge_base())
                self.assert_stage("needs_implementation")
        self.item.file("verify.md").write_text(VERIFY)
        self.assert_stage("unreviewed")

    def test_legacy_and_submitted_self_review_routing_stays_unchanged(self):
        self.value()
        template = (ROOT / "lines/oss/templates/verify.md").read_text()
        for schema, pr_url in ((1, None), (2, "https://github.com/o/r/pull/1")):
            with self.subTest(schema=schema, pr_url=pr_url):
                self.item.meta.update(schema=schema, pr_url=pr_url)
                self.item.save()
                for text in (template, "verdict: fix-needed\n", "verdict: drop\n"):
                    self.item.file("verify.md").write_text(text)
                    self.assertEqual(plan.classify(self.item)[0], "unreviewed")

    def test_stacked_branch_compares_with_its_own_base(self):
        self.value()
        self.item.file("verify.md").write_text(VERIFY)
        self.item.meta["stacked_on"] = "contrib/fix"
        self.item.save()
        self.assert_stage("needs_implementation")

    def test_missing_base_is_a_repair_not_permission_to_build(self):
        self.value()
        self.item.file("verify.md").write_text(VERIFY)
        self.item.meta["base"] = "absent"
        self.item.save()
        entry = self.assert_stage("errors")
        self.assertIn("base", entry["error"])

    def test_posted_proposal_waits_without_reposting_or_building(self):
        self.value("propose")
        self.item.file("proposal.md").write_text("Question for maintainers.\n")
        self.item.meta.update(status="awaiting-maintainer", proposal_url="https://github.com/o/r/issues/1#issuecomment-2")
        self.item.save()
        report = plan.run()
        self.assertFalse(any(entries for key, entries in report.items() if isinstance(entries, list)))

    def test_proposal_opened_as_its_own_issue_waits_without_a_second_post(self):
        self.value("propose")
        self.item.file("proposal.md").write_text("Question for maintainers.\n")
        self.item.meta.update(status="awaiting-maintainer", tracking_issue="https://github.com/o/r/issues/7", issue=7)
        self.item.save()
        report = plan.run()
        self.assertFalse(any(entries for key, entries in report.items() if isinstance(entries, list)))

    def test_own_issue_only_counts_as_the_proposal_when_the_value_review_said_propose(self):
        self.value()
        self.item.meta.update(status="awaiting-maintainer", tracking_issue="https://github.com/o/r/issues/7", issue=7)
        self.item.save()
        self.assert_stage("needs_implementation")
        self.value("propose")
        self.item.file("proposal.md").write_text("Question for maintainers.\n")
        self.item.meta.update(status="preparing")
        self.item.save()
        self.assert_stage("proposal_ready")

    def test_inactive_and_submitted_items_keep_their_existing_routes(self):
        self.item.file("demand.json").unlink()
        for status in ("parked", "blocked", "dropped", "closed", "abandoned", "merged", "submitted"):
            with self.subTest(status=status):
                self.item.meta.update(status=status, pr_url="https://github.com/o/r/pull/1" if status == "submitted" else None)
                self.item.save()
                report = plan.run()
                populated = [key for key, entries in report.items() if isinstance(entries, list) and entries]
                self.assertEqual(populated, ["parked_or_blocked"] if status in ("parked", "blocked") else [])


class Influence(Workspace):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.stars = {"o/big": 5000, "o/small": 10}
        self.contributors = {"o/big": ["someone"] * 240 + ["Fixture-Owner"] + ["other"] * 19}
        self.followers = 7
        self.profile_fails = False
        mocker = patch.object(plan, "gh_json", side_effect=self.respond)
        mocker.start()
        self.addCleanup(mocker.stop)
        write_json(self.ledger, [
            row("o/big", 1, "merged", ago(9), ago(8)), row("o/big", 2, "merged", ago(9), ago(8)), row("o/big", 3, "submitted", ago(1)),
            row("o/small", 1, "merged", ago(9), ago(8)),
            row("o/broken", 1, "merged", ago(9), ago(8)),
            row("o/none", 1, "submitted", ago(1)),
        ])
        self.history = self.ledger.with_name("influence.jsonl")

    def respond(self, *args, **kwargs):
        self.calls.append(args)
        self.assertEqual(kwargs, {"timeout": GH_READ_TIMEOUT})
        self.assertEqual(args[0], "api")
        self.assertFalse(any(flag in args for flag in ("-X", "--method", "-f", "-F", "--input")))
        endpoint = args[1]
        if endpoint.startswith("users/"):
            if self.profile_fails:
                raise CommandError("gh api users/x failed (1): HTTP 502")
            return {"login": "fixture-owner", "followers": self.followers, "public_repos": 3}
        repo = "/".join(endpoint.split("?")[0].split("/")[1:3])
        if repo not in self.stars:
            raise CommandError(f"gh api repos/{repo} failed (1): HTTP 404")
        if endpoint.endswith(repo):
            self.assertEqual(args[2:], ("--jq", ".stargazers_count"))
            return self.stars[repo]
        page = int(endpoint.rsplit("page=", 1)[1])
        people = self.contributors.get(repo, ["someone"] * 1000)
        return [{"login": login, "type": "User"} for login in people[(page - 1) * 100:page * 100]]

    def test_ranks_span_pages_stop_at_500_and_one_failing_repo_is_an_error_entry(self):
        report = plan.influence()
        repos = {r["repo"]: r for r in report["repos"]}
        self.assertEqual([r["repo"] for r in report["repos"]], ["o/big", "o/small", "o/broken"])
        self.assertEqual(repos["o/big"], {"repo": "o/big", "merged": 2, "stars": 5000, "rank": 241, "contributors_seen": 260})
        self.assertEqual(repos["o/small"], {"repo": "o/small", "merged": 1, "stars": 10, "rank": None, "contributors_seen": 500})
        self.assertEqual(repos["o/broken"]["merged"], 1)
        self.assertIn("HTTP 404", repos["o/broken"]["error"])
        self.assertEqual((report["followers"], report["public_repos"], report["merged_total"]), (7, 3, 4))
        self.assertEqual(sum("o/small/contributors" in c[1] for c in self.calls), 5)
        self.assertEqual(sum("o/big/contributors" in c[1] for c in self.calls), 3)
        self.assertFalse(any("o/none" in c[1] for c in self.calls))
        self.assertIsNone(report["recorded"])
        self.assertFalse(self.history.exists())

    def test_record_appends_a_line_and_reports_deltas_against_the_previous_one(self):
        first = plan.influence(record=True)
        self.assertEqual(first["recorded"], str(self.history))
        self.assertIsNone(first["previous_at"])
        rows = json.loads(self.ledger.read_text()) + [row("o/big", 4, "merged", ago(2), ago(1)), row("o/new", 1, "merged", ago(2), ago(1))]
        write_json(self.ledger, rows)
        self.stars["o/new"] = 1
        self.contributors["o/big"] = ["someone"] * 199 + ["fixture-owner"]
        self.followers = 9
        code, second = self.run_cli("influence", "--record")
        self.assertEqual(code, 1)
        lines = [json.loads(line) for line in self.history.read_text().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["repos"], first["repos"])
        self.assertNotIn("delta", json.dumps(lines))
        repos = {r["repo"]: r for r in second["repos"]}
        self.assertEqual(repos["o/big"]["delta"], {"merged": 1, "rank": -41, "previous_rank": 241})
        self.assertEqual(repos["o/broken"]["delta"], {"merged": 0, "rank": None, "previous_rank": None})
        self.assertTrue(repos["o/new"]["delta"]["new"])
        self.assertEqual((second["followers_delta"], second["previous_at"]), (2, first["at"]))

    def test_record_is_refused_when_the_profile_lookup_fails(self):
        self.profile_fails = True
        code, report = self.run_cli("influence", "--record")
        self.assertEqual(code, 1)
        self.assertIn("profile lookup failed", report.get("record_refused", ""))
        self.assertIsNone(report["recorded"])
        self.assertFalse(self.history.exists())

    def test_record_is_refused_when_every_repo_lookup_fails(self):
        self.stars.clear()
        report = plan.influence(record=True)
        self.assertIn("every repo lookup failed", report.get("record_refused", ""))
        self.assertFalse(self.history.exists())

    def test_a_corrupt_history_line_refuses_instead_of_dropping_the_delta(self):
        self.history.write_text('{"at": "2026-11-03T00:00:00+00:00", "repos": []}\n{"at": "2026-11-04T0\n')
        with self.assertRaises(SystemExit) as raised:
            plan.influence(record=True)
        self.assertIn("not an influence reading", str(raised.exception))
        self.assertEqual(len(self.history.read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
