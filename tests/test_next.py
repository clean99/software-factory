import datetime as dt
import io
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import setup_module

from factory import cli
from factory.core import commit, lessons, nextaction, publication, shiftlock, timeline, verdicts
from factory.util import CONFIG, CommandError, Item, read_json, run, write_json

AT = dt.datetime(2026, 11, 10, 17, tzinfo=dt.timezone.utc)
HEAD = "a" * 40
NEXT_HEAD = "b" * 40
PLAN_RUN = nextaction.plan.run
FOLLOWUP_RUN = nextaction.followup.run
LANE_ORDER = nextaction.lanes.order


def setUpModule():
    setup_module(unittest)


class SavedMainRecovery(unittest.TestCase):
    def test_recovery_commands_refresh_a_stale_ref_after_an_up_to_date_push(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            remote, checkout = root / "remote.git", root / "checkout"
            def git(cwd, *args, **kwargs):
                return subprocess.run(["git", "-C", str(cwd), *map(str, args)], text=True,
                                      capture_output=True, check=True, **kwargs).stdout.strip()
            git(root, "init", "--bare", remote)
            git(root, "init", "-b", "main", checkout)
            git(checkout, "remote", "add", "origin", remote)
            tree = git(checkout, "hash-object", "-t", "tree", "--stdin", "-w", input="")
            identity = {**os.environ, "GIT_AUTHOR_NAME": "Fixture", "GIT_COMMITTER_NAME": "Fixture",
                        "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
            first = git(checkout, "commit-tree", tree, "-m", "First record", env=identity)
            second = git(checkout, "commit-tree", tree, "-m", "Second record", "-p", first, env=identity)
            git(checkout, "update-ref", "refs/heads/main", first)
            git(checkout, "push", "origin", "main")
            git(checkout, "update-ref", "refs/heads/main", second)
            # A literal URL push leaves the origin tracking ref at the earlier commit.
            git(checkout, "push", remote, "main")
            self.assertEqual(git(checkout, "rev-parse", "origin/main"), first)
            self.assertEqual(git(remote, "rev-parse", "main"), second)
            with patch.object(nextaction, "ROOT", checkout):
                recovery = nextaction.push_action()
                self.assertEqual(recovery["kind"], "push")
                self.assertEqual(git(checkout, "rev-parse", "origin/main"), first)
                self.assertTrue(all(entry[1] == "=" for entry in publication.planned_push(["origin", "main"], checkout)))
                for step in recovery["commands"]:
                    argv = shlex.split(step)
                    if argv[:2] == ["bin/factory", "push"]:
                        # This local fixture has no publication policy; retain the real push planner and wrapper.
                        with patch.object(publication, "check_push", return_value=[]):
                            self.assertEqual(publication.main("push", argv[2:]), 0)
                    elif argv[:1] == ["git"]:
                        subprocess.run(argv, capture_output=True, check=True)
                self.assertEqual(git(checkout, "rev-parse", "origin/main"), second)
                self.assertIsNone(nextaction.push_action())


class NextActions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name).resolve()
        self.runs = self.root / "runs"
        self.ledger = self.root / "ledger/prs.json"
        self.ledger.parent.mkdir()
        self.runs.mkdir()
        write_json(self.ledger, [])
        self.clock = AT
        self.plan = {key: [] for key in ("ship_ready", "needs_coordinator_review", "fix_round", "stale_reviews", "held",
                                        "unreviewed", "missing_clone", "errors", "repo_slots", "needs_value_review",
                                        "needs_implementation", "proposal_ready")}
        self.disk = SimpleNamespace(free=20 * 1024 ** 3)
        self.status = ""
        self.lanes = []
        self.exploration = []
        self.lesson_problems = []
        self.live_events = []

        def lane_order(lane=None):
            if lane == "depth":
                return self.lanes
            if lane == "explore":
                return self.exploration
            return self.lanes + self.exploration

        patches = [patch.dict(CONFIG, {"runs_dir": str(self.runs), "ledger_file": str(self.ledger), "repos_dir": str(self.root / "repos"),
                                      "limits": {**CONFIG["limits"], "max_hunts_per_shift": 2, "review_budget_minutes": 120}}),
                   patch.object(nextaction, "ROOT", self.root), patch.object(shiftlock, "ROOT", self.root),
                   patch.object(nextaction.clean, "ROOT", self.root),
                   patch.object(nextaction, "now", side_effect=lambda: self.clock),
                   patch.object(shiftlock, "now", side_effect=lambda: self.clock),
                   patch.object(timeline, "now", side_effect=lambda: self.clock),
                   patch.object(nextaction.review.time, "time", side_effect=lambda: self.clock.timestamp()),
                   patch.object(nextaction.plan, "run", side_effect=lambda: self.plan),
                   patch.object(nextaction.shutil, "disk_usage", side_effect=lambda _: self.disk),
                   patch.object(nextaction.lanes, "order", side_effect=lane_order),
                   patch.object(lessons, "lint", side_effect=lambda *args: self.lesson_problems),
                   patch.object(nextaction.followup, "events", side_effect=lambda: self.live_events),
                   patch.object(nextaction.followup, "run", side_effect=AssertionError("no implicit sweep")),
                   patch.object(nextaction.followup, "waiting_issues", side_effect=AssertionError("no implicit issue lookup")),
                   patch.object(nextaction, "run", side_effect=lambda argv, **kw: SimpleNamespace(returncode=1, stdout="")
                                if "check-ignore" in argv else SimpleNamespace(stdout=self.status)),
                   patch.object(Item, "head_sha", lambda item: item.meta.get("head", HEAD)),
                   patch.object(verdicts, "bound", lambda item, head, reviewed: bool(reviewed and head.startswith(reviewed)))]
        for mocker in patches:
            mocker.start()
            self.addCleanup(mocker.stop)

    def item(self, name, bucket=None, repo="o/r", **meta):
        path = self.runs / "2026-11-10" / name
        path.mkdir(parents=True)
        write_json(path / "item.json", {"repo": repo, "branch": name, "clone": str(self.root / "repos" / name),
                                        "status": "preparing", "head": HEAD, **meta})
        if bucket:
            self.plan[bucket].append({"item": str(path.relative_to(self.root)), "repo": repo, "head": HEAD,
                                      "reasons": ["needs current evidence"]})
        return Item(path)

    def review(self, item, verdict="ship", head=HEAD, coordinator=False):
        name = "coordinator-review.md" if coordinator else "review.md"
        item.file(name).write_text(f"head: {head}\nreviewer: independent\nverdict: {verdict}\n")

    def row(self, number=1, status="submitted", **extra):
        row = {"url": f"https://github.com/o/r/pull/{number}", "repo": "o/r", "number": number, "status": status,
               "head_sha": HEAD, "submitted_at": (self.clock - dt.timedelta(days=1)).isoformat(), **extra}
        rows = read_json(self.ledger)
        rows.append(row)
        write_json(self.ledger, rows)
        return row

    def event(self, kind, row=None, detail=None, key=None, **extra):
        row = row or {"url": "https://github.com/o/r/pull/1", "repo": "o/r", "number": 1}
        event = {"key": key or row["url"] + "#" + kind, "kind": kind, "url": row["url"], "repo": row["repo"],
                 "number": row.get("number"), "item": row.get("item"), "detail": detail or {"text": "Current observation"},
                 "at": self.clock.isoformat(), **extra}
        path = self.ledger.with_name("followup-events.jsonl")
        with path.open("a") as handle:
            handle.write(json.dumps(event) + "\n")
        return event

    def actions(self, kind=None):
        result = nextaction.actions(observe=False)
        return [a for a in result if a["kind"] == kind] if kind else result

    def wave(self):
        waves = self.actions("wave")
        return json.loads(next(c for c in waves[0]["commands"] if c.startswith('{"factory":'))) if waves else None

    def run_cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(list(args))
        return code, output.getvalue()

    def test_saved_main_commits_are_pushed_before_every_other_action(self):
        self.disk.free = 1
        responses = {"symbolic-ref": "main", "rev-parse": HEAD, "rev-list": NEXT_HEAD}
        def run(args, **kwargs):
            return SimpleNamespace(stdout=responses.get(args[3], ""))
        with patch.object(nextaction, "run", side_effect=run):
            first = self.actions()[0]
            self.assertEqual(first["kind"], "push")
            self.assertIn("-- origin main", first["commands"][0])
            responses["rev-list"] = ""
            self.assertEqual(self.actions()[0]["kind"], "disk")
            responses.update({"symbolic-ref": "feature", "rev-list": NEXT_HEAD})
            self.assertEqual(self.actions("push"), [])

    def test_all_record_destinations_precede_scouting_and_explain_profile_edits(self):
        self.row(status="merged")
        self.lanes = ["o/r"]
        self.plan["repo_slots"] = [{"repo": "o/r", "free": 1}]
        actions = self.actions()
        kinds = [a["kind"] for a in actions]
        self.assertLess(max(i for i, kind in enumerate(kinds) if kind == "records"), kinds.index("wave"))
        profile = next(a for a in actions if a["kind"] == "records" and a["target"] == "profile")
        self.assertIn("groups[].repos[]", profile["commands"][0])
        self.assertIn("blurb", profile["commands"][0])
        self.assertTrue(any("profile-check --readme" in c for c in profile["commands"]))

    def test_old_silence_lesson_does_not_cover_later_closing_feedback(self):
        row = self.row(status="closed", closed_at="2026-11-10T12:00:00Z", lesson="L-001")
        lesson_file = self.root / "lessons.md"
        lesson_file.write_text(f"## L-001 · Silence\n\n- source: {row['url']} (2026-11-01)\n")
        with patch.dict(CONFIG, lessons_file=str(lesson_file)):
            first = self.actions()[0]
            self.assertEqual(first["kind"], "lesson")
            self.assertIn("closing feedback", first["why"])
            lesson_file.write_text(f"## L-001 · Silence\n\n- source: {row['url']} (2026-11-10)\n")
            self.assertEqual(self.actions()[0]["kind"], "lesson")
            self.run_cli("lesson", "add", "--pr", row["url"], "--title", "Closing review",
                         "--rule", "Read closing evidence before another submission", "--scope", "o/r", "--source", row["url"])
            self.assertEqual(self.actions("lesson"), [])
            saved = read_json(self.ledger)[0]
            self.assertEqual(saved["lesson_closed_at"], row["closed_at"])

    def test_closing_review_uses_only_the_target_pr_citation_date(self):
        row = self.row(status="closed", closed_at="2026-11-10T12:00:00Z", lesson="L-001")
        lesson_file = self.root / "lessons.md"
        other = "https://github.com/other/project/pull/2"
        cases = (
            (f"{row['url']} (2026-11-01); {other} (2026-11-12)", True),
            (f"{row['url']}, {other} (2026-11-12)", True),
            (f"{other} (2026-11-12); {row['url']} (2026-11-01)", True),
            (f"{row['url']} (2026-11-12); {other} (2026-11-01)", False),
            (f"{row['url']}1 (2026-11-12)", True),
        )
        for source, owed in cases:
            with self.subTest(source=source), patch.dict(CONFIG, lessons_file=str(lesson_file)):
                lesson_file.write_text(f"## L-001 · Review\n\n- source: {source}\n")
                self.assertEqual(bool(self.actions("lesson")), owed)
        write_json(self.ledger, [{**row, "lesson_closed_at": row["closed_at"]}])
        with patch.dict(CONFIG, lessons_file=str(lesson_file)):
            self.assertEqual(self.actions("lesson"), [])

    def test_provenance_and_validation_commands_have_no_sentence_suffix(self):
        self.lanes = ["o/r"]
        self.plan["repo_slots"] = [{"repo": "o/r", "free": 1}]
        commands = self.actions("wave")[0]["commands"]
        provenance = next(c for c in commands if "bin/factory provenance" in c)
        self.assertTrue(provenance.endswith("<transcript dir>"), provenance)
        item = self.item("review-copy", schema=2)
        done = nextaction.review_done(item, "Finish the review.")
        self.assertEqual(done.splitlines()[-1], nextaction.command("review", "check", "--item", nextaction.item_target(item), "review"))

    def test_release_precedes_record_commit_and_requests_same_closeout(self):
        self.status = " M ledger/prs.json\0"
        release = nextaction.action("release", "idle", [], "Release", "", [], "")
        with patch.object(nextaction, "release_action", return_value=release):
            self.assertEqual([a["kind"] for a in self.actions()], ["release", "commit", "report"])
        with patch.object(nextaction.clean, "checkouts", return_value=[self.root]), \
                patch.object(nextaction.clones, "release_idle", return_value={"reports": [{"checkout": str(self.root), "would_release": True}]}), \
                patch.object(nextaction.clean, "size", return_value=1):
            self.assertIn("commit action", nextaction.release_action(set())["commands"][1])

    def test_repairs_close_when_disk_and_plan_recover(self):
        self.disk.free = 5 * 1024 ** 3
        self.plan["missing_clone"] = [{"item": "runs/day/gone", "clone": "workspace/repos/gone"}]
        self.plan["errors"] = [{"item": "runs/day/bad", "error": "branch missing"}]
        repairs = self.actions("repair")
        self.assertEqual(len(repairs), 2)
        self.assertIn("clone-heal", " ".join(repairs[0]["commands"]))
        self.assertEqual(len(self.actions("disk")), 1)
        self.disk.free = 6 * 1024 ** 3
        self.plan["missing_clone"].clear()
        self.plan["errors"].clear()
        self.assertEqual(self.actions("repair"), [])
        self.assertEqual(self.actions("disk"), [])

    def test_disk_shortage_is_acknowledged_for_one_half_day(self):
        self.disk.free = 3 * 1024 ** 3
        self.status = " M tracked.txt\0"
        shortage = self.actions()[0]
        self.assertEqual(shortage["kind"], "disk")
        with self.assertRaisesRegex(ValueError, "free space left"):
            nextaction.acknowledge(shortage["id"], "tried the cleanup")
        self.assertEqual(self.actions()[0]["id"], shortage["id"])
        nextaction.acknowledge(shortage["id"], "3.0 GiB free after clean --prune; the rest belongs to other sessions")
        self.assertEqual(self.actions("disk"), [])
        self.assertEqual(self.actions()[0]["kind"], "commit")
        self.clock += dt.timedelta(hours=6)
        self.assertEqual(self.actions("disk"), [])
        self.clock += dt.timedelta(hours=2)
        returned = self.actions()[0]
        self.assertEqual(returned["kind"], "disk")
        self.assertNotEqual(returned["id"], shortage["id"])

    def test_owner_action_closes_only_when_pr_url_is_recorded(self):
        row = self.row(cla_state="pending")
        self.event("cla", row)
        entry = self.actions("owner-action")[0]
        with self.assertRaisesRegex(ValueError, "observed state"):
            nextaction.acknowledge(entry["id"], "will do later")
        path = self.root / "docs/owner-actions.md"
        path.parent.mkdir()
        path.write_text("# Owner actions\n\n" + row["url"])
        self.assertEqual(self.actions("owner-action"), [])

    def test_ci_ack_requires_each_job_classification_and_evidence(self):
        row = self.row()
        jobs = [{"name": "types (5.0)", "head_sha": HEAD, "run_id": "45"}, {"name": "test", "head_sha": HEAD}]
        self.event("ci-failing", row, {"jobs": jobs, "head_sha": HEAD})
        entry = self.actions("ci-triage")[0]
        self.assertIn("--log-failed", entry["commands"][0])
        for note in ("all external", "types (5.0): external — base run 43 matches\ntest: ours"):
            with self.assertRaisesRegex(ValueError, "concrete log/base evidence"):
                nextaction.acknowledge(entry["id"], note)
        note = "types (5.0): external — base run 43 has the same error\ntest: ours — run 45 fails on the changed assertion"
        nextaction.acknowledge(entry["id"], note)
        self.assertEqual(self.actions("ci-triage"), [])
        self.assertEqual(read_json(self.root / nextaction.ACKS)[entry["id"]]["note"], note)
        self.event("ci-failing", row, {"jobs": [{"name": "new job", "head_sha": HEAD}], "head_sha": HEAD}, key="new-failure")
        self.assertNotEqual(self.actions("ci-triage")[0]["id"], entry["id"])

    def test_new_head_invalidates_ci_ack_and_supersedes_old_failure(self):
        row = self.row()
        self.event("ci-failing", row, {"head_sha": HEAD, "jobs": [{"name": "test"}]})
        old = self.actions("ci-triage")[0]
        nextaction.acknowledge(old["id"], "test: external — the base log shows the same service outage")
        write_json(self.ledger, [{**row, "head_sha": NEXT_HEAD}])
        self.event("ci-failing", row, {"head_sha": NEXT_HEAD, "jobs": [{"name": "test"}]}, key="new-head")
        self.assertNotEqual(self.actions("ci-triage")[0]["id"], old["id"])
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            nextaction.acknowledge(old["id"], "test: external — old evidence")
        write_json(self.ledger, [{**row, "head_sha": NEXT_HEAD, "checks": {"failing": []}, "last_check": (self.clock + dt.timedelta(minutes=1)).isoformat()}])
        self.assertEqual(self.actions("ci-triage"), [])

    def test_event_decisions_are_acked_individually(self):
        for kind in ("stale-pr-head", "issue-taken", "issue-confirmed"):
            self.event(kind)
        entries = [a for a in self.actions() if a["kind"] in {"stale-head", "issue-taken", "issue-confirmed"}]
        self.assertEqual(len(entries), 3)
        for entry in entries:
            nextaction.acknowledge(entry["id"], "Read live state; preserved the branch and recorded the disposition")
        self.assertFalse(any(a["kind"] in nextaction.ACKABLE - {"report"} for a in self.actions()))
        self.event("issue-confirmed", key="new-maintainer-comment")
        self.assertEqual(len(self.actions("issue-confirmed")), 1)

    def test_dropping_a_taken_issue_settles_it_without_an_ack(self):
        item = self.item("taken", status="waiting")
        self.event("issue-taken", item=str(item.dir))
        entry = self.actions("issue-taken")[0]
        self.assertIn("settles the action without ack", "\n".join(entry["commands"]))
        output = io.StringIO()
        with redirect_stdout(output):
            nextaction.print_action(entry)
        self.assertNotIn("\nbin/factory ack ", output.getvalue())
        item.meta.update(status="dropped", dropped_reason="Competing PR https://github.com/o/r/pull/2 solves it")
        item.save()
        self.assertEqual(self.actions("issue-taken"), [])
        self.assertFalse((self.root / nextaction.ACKS).exists())

    def test_state_changing_ack_actions_only_print_a_conditional_ack(self):
        for kind in sorted(nextaction.ACKABLE - {"report"}) + ["repair"]:
            with self.subTest(kind=kind):
                entry = nextaction.action(kind, "target", {}, "Action", "Evidence", [], "Result")
                if kind == "repair":
                    entry["observation"] = True
                output = io.StringIO()
                with redirect_stdout(output):
                    nextaction.print_action(entry)
                self.assertIn("Only if this ID is still pending after bin/factory next: bin/factory ack", output.getvalue())
                self.assertNotIn("\nbin/factory ack ", output.getvalue())

    def test_other_event_decisions_close_when_the_item_becomes_inactive(self):
        for kind, name in (("stale-pr-head", "stale-head"), ("issue-confirmed", "issue-confirmed")):
            with self.subTest(kind=kind):
                item = self.item(kind)
                self.event(kind, item=str(item.dir))
                entry = self.actions(name)[0]
                self.assertIn("only if this ID is still pending", entry["done_when"])
                item.meta.update(status="parked", parked_reason="Waiting for an upstream decision")
                item.save()
                self.assertEqual(self.actions(name), [])

    def test_coordinator_ship_and_held_use_observed_state(self):
        item = self.item("ready", "needs_coordinator_review")
        self.review(item)
        first = self.actions("coordinator-review")[0]
        self.assertEqual(first["brief"], "bin/factory brief coordinator-review --item runs/2026-11-10/ready")
        item.meta["head"] = NEXT_HEAD
        item.save()
        self.assertNotEqual(self.actions("coordinator-review")[0]["id"], first["id"])
        self.review(item, head=NEXT_HEAD, coordinator=True)
        self.plan["ship_ready"] = self.plan["needs_coordinator_review"]
        self.plan["needs_coordinator_review"] = []
        self.assertEqual(self.actions("coordinator-review"), [])
        self.assertEqual(self.actions("ship")[0]["commands"], ["bin/factory ship runs/2026-11-10/ready --dry-run", "bin/factory ship runs/2026-11-10/ready"])
        item.meta["pr_url"] = "https://github.com/o/r/pull/1"
        item.save()
        self.assertEqual(self.actions("ship"), [])
        held = self.item("held", "held")
        self.assertIn("needs current evidence", self.actions("held")[0]["why"])
        held.meta.update(status="parked", parked_reason="No supported use")
        held.save()
        self.assertEqual(self.actions("held"), [])

    def test_coordinator_fix_needed_closes_review_without_shipping(self):
        item = self.item("fix", "needs_coordinator_review")
        self.review(item)
        self.review(item, verdict="fix-needed", coordinator=True)
        self.assertEqual(self.actions("coordinator-review"), [])
        self.assertEqual(self.actions("ship"), [])
        self.assertEqual(len(self.actions("held")), 1)

    def test_takeover_after_round_and_after_budget_then_closes(self):
        item = self.item("round", "fix_round")
        self.review(item, verdict="fix-needed")
        item.file("review-round1.md").write_text("verdict: fix-needed")
        self.assertEqual(len(self.actions("takeover")), 1)
        self.assertIsNone(self.wave())
        self.review(item)
        self.assertEqual(self.actions("takeover"), [])
        timed = self.item("timed", "unreviewed")
        write_json(timed.file(timeline.FILE_NAME), [{"event": "review-started", "at": (self.clock - dt.timedelta(hours=3)).isoformat()}])
        self.assertEqual(self.actions("takeover"), [])
        write_json(timed.file(timeline.FILE_NAME), [{"event": "review-started", "head": HEAD,
                                                   "at": (self.clock - dt.timedelta(hours=3)).isoformat()}])
        self.assertEqual(len(self.actions("takeover")), 1)
        timed.meta.update(status="parked", parked_reason="Review needs a new scope")
        timed.save()
        self.assertEqual(self.actions("takeover"), [])

    def test_wave_selects_roles_slots_rounds_and_hunt_limit(self):
        for name, bucket, verdict in (("fix", "fix_round", "fix-needed"), ("stale", "stale_reviews", "ship")):
            self.review(self.item(name, bucket, repo="o/busy"), verdict=verdict)
        unreviewed = self.item("unreviewed", "unreviewed", repo="o/busy")
        unreviewed.file("verify.md").write_text("verdict: ship\n")
        self.item("unfinished", "unreviewed", repo="o/busy")
        repeated = self.item("repeated", "fix_round", repo="o/repeated")
        self.review(repeated, "fix-needed")
        repeated.file("review-round1.md").write_text(f"head: {HEAD}\nverdict: fix-needed\n")
        self.lanes = ["o/busy", "o/stopped", "o/full", "o/never", "o/one", "o/two", "o/three"]
        self.plan["repo_slots"] = [{"repo": r, "free": 0 if r == "o/full" else 1, "stopped": r == "o/stopped"}
                                   for r in self.lanes if r != "o/never"]
        wave = self.wave()
        self.assertEqual(wave["concurrency"], 3)
        self.assertEqual(wave["factory"], str(self.root))
        self.assertEqual([(t["role"], pathlib.Path(t["item"]).name) for t in wave["tasks"] if "item" in t],
                         [("fix", "fix"), ("review", "stale"), ("review", "unreviewed")])
        self.assertEqual([t["repo"] for t in wave["tasks"] if t["role"] == "scout"], ["o/never", "o/one"])
        self.assertIn("--ttl-hours 8", self.actions("wave")[0]["commands"][0])
        self.assertIn("bin/factory provenance", " ".join(self.actions("wave")[0]["commands"]))

    def demand(self, item, decision="build"):
        write_json(item.file("demand.json"), {"author": "scout", "claim": "Supported input is lost.",
            "who_hits_it": {"kind": "external-report", "source": "https://github.com/o/r/issues/1",
                            "quote": "My input was lost.", "self_filed": False},
            "contract": {}, "workaround": "Preserve the input manually.", "error_path": False,
            "real_occurrence": "", "decision": decision})

    def value(self, item, verdict="build", **fields):
        write_json(item.file("value-review.json"), {"reviewer": "independent", "complete": True,
            "demand_sha256": verdicts.demand_sha256(item), "verdict": verdict,
            "answers": {key: {"ok": True, "note": "Checked the external report."}
                        for key in nextaction.review.VALUE_ANSWERS}, **fields})

    def test_real_proposal_resumes_value_review_then_coordinator_posting(self):
        item = self.item("proposal with space", schema=2, issue=1, target="issue")
        self.demand(item, "propose")
        item.file("proposal.md").write_text("Plan, API sketch, scope, tests and a question.\n")
        target = nextaction.item_target(item)
        with patch.object(nextaction.plan, "run", side_effect=PLAN_RUN):
            self.assertEqual([(t["role"], t["item"]) for t in self.wave()["tasks"]], [("value-review", target)])
            self.assertEqual(self.actions("held"), [])
            self.assertEqual(self.actions("repair"), [])
            self.assertIn(" value", self.actions("wave")[0]["done_when"])
            self.assertNotIn(f"--item {shlex.quote(target)} review", self.actions("wave")[0]["done_when"])
            self.value(item, "propose")
            self.assertIsNone(self.wave())
            proposal, = self.actions("propose")
            self.assertEqual(proposal["title"], "Post the proposal for " + item.dir.name)
            self.assertEqual(proposal["why"], "value-review.json verdict: propose")
            for text in ("proposal.md", "demand.json", "cited sources", "correct it yourself"):
                self.assertIn(text, proposal["commands"][0])
            self.assertEqual([shlex.split(text) for text in proposal["commands"][1:]], [
                ["bin/factory", "approve-proposal", target, "--note", CONFIG["standing_approval_note"]],
                ["bin/factory", "propose", target]])
            self.assertIn("proposal_ready", proposal["done_when"])
            with self.assertRaisesRegex(ValueError, "observed state"):
                nextaction.acknowledge(proposal["id"], "skip")
            item.file("proposal.md").write_text("A corrected question.\n")
            self.assertNotEqual(self.actions("propose")[0]["id"], proposal["id"])
            item.meta.update(proposal_url="https://github.com/o/r/issues/1#issuecomment-2", status="awaiting-maintainer")
            item.save()
            self.assertEqual(self.actions("propose"), [])
            self.assertEqual(self.actions("held"), [])
            self.assertIsNone(self.wave())

    def test_real_build_resumes_each_missing_stage(self):
        item = self.item("build", schema=2)
        self.demand(item)
        target = nextaction.item_target(item)
        with patch.object(nextaction.plan, "run", side_effect=PLAN_RUN), patch.object(Item, "merge_base", return_value=NEXT_HEAD):
            self.assertEqual([(t["role"], t["item"]) for t in self.wave()["tasks"]], [("value-review", target)])
            self.value(item)
            self.assertEqual([(t["role"], t["item"]) for t in self.wave()["tasks"]], [("implement", target)])
            item.clone.mkdir(parents=True)
            item.file("verify.md").write_text("verdict: ship\n")
            self.assertEqual([(t["role"], t["item"]) for t in self.wave()["tasks"]], [("review", target)])
            self.assertEqual(self.actions("held"), [])
            self.assertEqual(self.actions("repair"), [])

    def test_unfinished_self_review_keeps_an_implement_task_with_its_verdict(self):
        item = self.item("unfinished", schema=2)
        self.demand(item)
        self.value(item)
        item.clone.mkdir(parents=True)
        template = (ROOT / "lines/oss/templates/verify.md").read_text()
        with patch.object(nextaction.plan, "run", side_effect=PLAN_RUN), patch.object(Item, "merge_base", return_value=NEXT_HEAD):
            for text, verdict in ((template, "missing"), ("verdict: fix-needed\n", "fix-needed"), ("verdict: drop\n", "drop")):
                with self.subTest(verdict=verdict):
                    item.file("verify.md").write_text(text)
                    wave = self.wave()
                    self.assertIsNotNone(wave)
                    task, = wave["tasks"]
                    self.assertEqual((task["role"], task["item"]), ("implement", nextaction.item_target(item)))
                    self.assertIn(f"verify.md self-review verdict: {verdict}", task.get("note", ""))
                    self.assertEqual(nextaction.code_items(), {item.dir})

    def test_real_stage_holds_name_the_exact_repair(self):
        item = self.item("candidate", schema=2)
        with patch.object(nextaction.plan, "run", side_effect=PLAN_RUN):
            for part in ("demand", "value"):
                self.demand(item, "drop" if part == "demand" else "build")
                if part == "value":
                    self.value(item, "drop")
                held, = self.actions("held")
                expected = ("the scout dropped this candidate: set status to dropped in item.json with the reason"
                            if part == "demand" else "the value review says drop: set status to dropped in item.json and copy its reason")
                self.assertEqual(held["why"], expected)
                self.assertIsNone(self.wave())
            self.value(item, "propose")
            self.assertEqual(self.actions("held")[0]["why"], "write proposal.md as the value-review card describes")
            self.assertIsNone(self.wave())

    def test_prebuild_tasks_do_not_read_code_review_or_require_a_branch(self):
        for name, bucket in (("value", "needs_value_review"), ("build", "needs_implementation"), ("proposal", "proposal_ready")):
            item = self.item(name, bucket, schema=2)
            self.demand(item)
            if bucket != "needs_value_review":
                self.value(item, "propose" if bucket == "proposal_ready" else "build")
            item.file("proposal.md").write_text("Question.\n")
            item.file("review.json").write_text("{")
            item.file("coordinator-review.json").write_text("{")
        with patch.object(Item, "head_sha", side_effect=AssertionError("prebuild does not need code")):
            self.assertEqual([t["role"] for t in self.wave()["tasks"]], ["value-review", "implement"])
            self.assertEqual(len(self.actions("propose")), 1)
            self.assertEqual(self.actions("held") + self.actions("repair"), [])

    def test_proposal_is_ordered_after_ship_and_before_takeover(self):
        item = self.item("proposal", "proposal_ready", schema=2)
        self.demand(item, "propose")
        self.value(item, "propose")
        item.file("proposal.md").write_text("Question.\n")
        self.item("ready", "ship_ready")
        repeated = self.item("repeated", "fix_round")
        self.review(repeated, "fix-needed")
        repeated.file("review-round1.md").write_text("verdict: fix-needed\n")
        self.assertEqual([a["kind"] for a in self.actions()], ["ship", "propose", "takeover", "report"])

    def test_code_items_include_implemented_work_but_not_proposals_or_parked_items(self):
        implemented = self.item("implemented", schema=2)
        self.demand(implemented)
        self.value(implemented)
        implemented.clone.mkdir(parents=True)
        implemented.file("verify.md").write_text("verdict: ship\n")
        value = self.item("value", schema=2)
        self.demand(value, "propose")
        proposal = self.item("proposal", schema=2)
        self.demand(proposal, "propose")
        self.value(proposal, "propose")
        proposal.file("proposal.md").write_text("Question.\n")
        self.item("parked", schema=2, status="parked")
        self.event("issue-confirmed")
        with patch.object(nextaction.plan, "run", side_effect=PLAN_RUN), patch.object(Item, "merge_base", return_value=NEXT_HEAD), \
                patch.object(nextaction.followup, "events", side_effect=AssertionError("use saved observations")):
            before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
            self.assertEqual(nextaction.code_items(), {implemented.dir})
            self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_code_items_select_all_and_only_code_roles_and_actions(self):
        expected = set()
        tasks = []
        entries = []
        for role in ("scout", "value-review", "implement", "review", "fix", "follow", "takeover", "record"):
            item = self.item("task-" + role)
            tasks.append({"role": role, "item": nextaction.item_target(item)})
            if role in {"implement", "review", "fix", "follow", "takeover"}:
                expected.add(item.dir)
        tasks.append({"role": "scout", "repo": "o/r"})
        entries.append({"kind": "wave", "commands": ["Run wave", json.dumps({"factory": str(self.root), "tasks": tasks})]})
        for kind in ("coordinator-review", "ship", "takeover", "propose", "held", "repair", "report"):
            item = self.item("action-" + kind)
            entries.append({"kind": kind, "target": str(item.dir)})
            if kind in {"coordinator-review", "ship", "takeover"}:
                expected.add(item.dir)
        with patch.object(nextaction, "actions", return_value=entries) as actions:
            self.assertEqual(nextaction.code_items(), expected)
            actions.assert_called_once_with(observe=False, include_release=False)

    def test_hunts_are_bounded_across_waves_and_empty_scout_can_be_acked(self):
        self.lanes = ["o/one", "o/two", "o/three"]
        self.plan["repo_slots"] = [{"repo": r, "free": 1, "stopped": False} for r in self.lanes]
        scout = self.wave()["tasks"][0]
        nextaction.acknowledge(scout["key"], "No candidate: read all maintainer invitations; every issue is claimed")
        self.item("new", repo="o/two", created_at=self.clock.isoformat(), pr_url="https://github.com/o/two/pull/1")
        self.assertIsNone(self.wave())
        self.clock += dt.timedelta(days=1)
        self.assertIsNotNone(self.wave())

    def test_scouts_rotate_across_generations_by_last_scout_then_lane_order(self):
        self.lanes = ["o/one", "o/two", "o/three", "o/four", "o/five"]
        self.plan["repo_slots"] = [{"repo": r, "free": 1, "merged": 1} for r in self.lanes]
        first = self.wave()["tasks"]
        self.assertEqual([task["repo"] for task in first], self.lanes[:2])
        nextaction.acknowledge(first[0]["key"], "All examined requests are claimed")
        # A productive scout counts even when its candidate is later dropped.
        self.item("candidate", repo="o/two", created_at=self.clock.isoformat(), status="dropped")
        self.assertIsNone(self.wave())
        saved = read_json(self.root / nextaction.ACKS)
        self.assertEqual(saved[first[0]["key"]]["repo"], "o/one")
        self.clock += dt.timedelta(days=4)
        second = self.wave()["tasks"]
        self.assertEqual([task["repo"] for task in second], self.lanes[2:4])
        for task in second:
            nextaction.acknowledge(task["key"], "No wanted unclaimed candidate")
        self.clock += dt.timedelta(days=4)
        # The never-scouted lane wins, followed by the oldest (tied in lane order).
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/five", "o/one"])

    def test_empty_scout_cools_down_between_same_day_leases_until_three_days(self):
        self.lanes = ["o/r"]
        self.assertTrue(shiftlock.run("acquire", "first")["ok"])
        task, = self.wave()["tasks"]
        nextaction.acknowledge(task["key"], "No unclaimed candidate")
        self.assertTrue(shiftlock.run("release", "first")["ok"])
        self.clock += dt.timedelta(hours=1)
        self.assertTrue(shiftlock.run("acquire", "second")["ok"])
        self.assertIsNone(self.wave())
        self.assertTrue(shiftlock.run("release", "second")["ok"])
        self.clock += dt.timedelta(days=3, hours=-1, seconds=-1)
        self.assertIsNone(self.wave())
        self.clock += dt.timedelta(seconds=1)
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/r"])

    def test_new_saved_upstream_feedback_breaks_cooldown_but_rereads_do_not(self):
        self.lanes = ["o/r"]
        event = self.event("community-comment", key="old-comment")
        task, = self.wave()["tasks"]
        nextaction.acknowledge(task["key"], "No suitable candidate after reading this feedback")
        self.clock += dt.timedelta(days=1)
        self.event("community-comment", key=event["key"])
        self.event("repo-mergers-read", key="new-poll")
        self.assertIsNone(self.wave())
        self.event("maintainer-comment", key="new-invitation")
        task, = self.wave()["tasks"]
        self.assertEqual(task["repo"], "o/r")
        nextaction.acknowledge(task["key"], "Invitation is already claimed")
        self.clock += dt.timedelta(days=1)
        self.assertIsNone(self.wave())

    def test_new_merge_breaks_cooldown_without_any_network_read(self):
        self.lanes = ["o/r"]
        task, = self.wave()["tasks"]
        nextaction.acknowledge(task["key"], "No suitable candidate")
        self.clock += dt.timedelta(days=1)
        self.assertIsNone(self.wave())
        self.row(status="merged", merged_at=self.clock.isoformat())
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/r"])

    def test_scout_history_handles_case_renames_and_preserves_legacy_current_shift_ack(self):
        self.lanes = ["o/current", "o/other"]
        first, second = self.wave()["tasks"]
        write_json(self.root / nextaction.ACKS, {
            first["key"]: {"at": self.clock.isoformat(), "note": "Legacy no candidate"},
            "scout-older": {"at": (self.clock - dt.timedelta(days=1)).isoformat(), "repo": "O/OLD", "note": "No candidate"},
        })
        with patch.dict(CONFIG, {"repo_renames": {"o/old": "o/current"}}):
            self.assertEqual(self.wave()["tasks"], [second])
            self.clock += dt.timedelta(days=1)
            self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/other"])

    def test_active_exploration_uses_first_merge_slot_and_respects_stale_or_full_slots(self):
        self.exploration = ["o/probe-full", "o/stale", "o/new"]
        self.row(repo="o/probe-full")
        self.row(number=2, repo="o/stale", submitted_at=(self.clock - dt.timedelta(days=4)).isoformat())
        with patch.object(nextaction.plan, "now", return_value=self.clock):
            self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new"])

    def test_real_active_exploration_waits_for_depth_and_is_limited_to_one(self):
        def lane(repo):
            return {"repo": repo, "state": "active", "reason": "Fixture", "since": "2026-11-01"}
        path = self.root / "lines/oss/lanes.json"
        path.parent.mkdir(parents=True)
        write_json(path, {"depth": [lane("o/depth")], "explore": [lane("o/new1"), lane("o/new2")]})
        write_json(self.root / nextaction.ACKS, {
            "scout-prior": {"repo": "o/depth", "at": (self.clock - dt.timedelta(days=4)).isoformat(), "note": "No candidate"}})
        self.assertEqual(read_json(self.ledger), [])
        with patch.object(nextaction.lanes, "ROOT", self.root), patch.object(nextaction.lanes, "order", LANE_ORDER), \
                patch.object(nextaction.plan, "run", side_effect=PLAN_RUN), \
                patch.dict(CONFIG["limits"], {"max_hunts_per_shift": 3}):
            depth, = self.wave()["tasks"]
            self.assertEqual(depth["repo"], "o/depth")
            nextaction.acknowledge(depth["key"], "No suitable depth candidate")
            exploration, = self.wave()["tasks"]
            self.assertEqual(exploration["repo"], "o/new1")
            nextaction.acknowledge(exploration["key"], "No suitable exploration candidate")
            self.assertIsNone(self.wave())
            self.clock += dt.timedelta(days=4)
            depth, = self.wave()["tasks"]
            self.assertEqual(depth["repo"], "o/depth")
            nextaction.acknowledge(depth["key"], "No suitable depth candidate")
            self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new2"])

    def test_exploration_requires_each_depth_lane_to_be_empty_or_cooling_down(self):
        self.lanes = ["o/one", "o/two"]
        self.exploration = ["o/new"]
        with patch.dict(CONFIG["limits"], {"max_hunts_per_shift": 3}):
            first, second = self.wave()["tasks"]
            nextaction.acknowledge(first["key"], "No suitable candidate")
            self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/two"])
            nextaction.acknowledge(second["key"], "No suitable candidate")
            self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new"])
            self.clock += dt.timedelta(days=1)
            # Yesterday's empty hunts satisfy the depth condition while cooling down.
            self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new"])

    def test_productive_depth_hunt_does_not_unlock_exploration(self):
        self.lanes = ["o/depth"]
        self.exploration = ["o/new1", "o/new2"]
        depth = self.wave()["tasks"][0]
        nextaction.acknowledge(depth["key"], "No suitable candidate")
        # A saved candidate overrides even an earlier empty acknowledgement.
        self.item("candidate", repo="o/depth", created_at=self.clock.isoformat(), status="dropped")
        self.assertIsNone(self.wave())
        self.clock += dt.timedelta(days=1)
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/depth"])

    def test_productive_exploration_hunt_consumes_the_one_repo_allowance(self):
        self.exploration = ["o/new1", "o/new2"]
        self.item("candidate", repo="o/new1", created_at=self.clock.isoformat(), status="dropped")
        self.assertIsNone(self.wave())

    def test_legacy_current_shift_empty_ack_unlocks_only_one_exploration(self):
        self.lanes = ["o/depth"]
        self.exploration = ["o/new1", "o/new2"]
        depth = self.wave()["tasks"][0]
        write_json(self.root / nextaction.ACKS, {depth["key"]: {"at": self.clock.isoformat(), "note": "No candidate"}})
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new1"])

    def test_full_depth_lane_unlocks_exploration_but_unfinished_work_with_room_does_not(self):
        self.lanes = ["o/depth"]
        self.exploration = ["o/new"]
        self.plan["repo_slots"] = [{"repo": "o/depth", "free": 0}]
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new"])
        self.plan["repo_slots"] = []
        self.item("unfinished", repo="o/depth")
        self.assertIsNone(self.wave())

    def test_mixed_depth_conditions_unlock_only_one_exploration_hunt(self):
        self.lanes = ["o/full", "o/cooling", "o/remaining"]
        self.exploration = ["o/new1", "o/new2"]
        self.plan["repo_slots"] = [{"repo": "o/full", "free": 0}]
        write_json(self.root / nextaction.ACKS, {
            "scout-prior": {"repo": "o/cooling", "at": (self.clock - dt.timedelta(days=1)).isoformat(), "note": "No candidate"}})
        depth, = self.wave()["tasks"]
        self.assertEqual(depth["repo"], "o/remaining")
        nextaction.acknowledge(depth["key"], "No suitable depth candidate")
        exploration, = self.wave()["tasks"]
        self.assertEqual(exploration["repo"], "o/new1")
        nextaction.acknowledge(exploration["key"], "No suitable exploration candidate")
        self.assertIsNone(self.wave())

    def test_new_upstream_activity_reopens_depth_before_exploration(self):
        self.lanes = ["o/depth"]
        self.exploration = ["o/new"]
        depth, = self.wave()["tasks"]
        nextaction.acknowledge(depth["key"], "No suitable depth candidate")
        self.clock += dt.timedelta(days=1)
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new"])
        self.event("maintainer-comment", row={"repo": "o/depth", "url": "https://github.com/o/depth/pull/1"})
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/depth"])

    def test_productive_depth_can_unlock_exploration_only_when_its_slot_is_full(self):
        self.lanes = ["o/depth"]
        self.exploration = ["o/new"]
        self.item("candidate", repo="o/depth", created_at=self.clock.isoformat(), pr_url="https://github.com/o/depth/pull/1")
        self.row(repo="o/depth", status="merged")
        self.plan["repo_slots"] = [{"repo": "o/depth", "free": 1}]
        self.assertIsNone(self.wave())
        self.plan["repo_slots"][0]["free"] = 0
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new"])

    def test_shared_first_merge_pool_blocks_exploration_not_proven_depth(self):
        self.lanes = ["o/depth"]
        self.exploration = ["o/new"]
        self.row(repo="o/depth", status="merged")
        self.row(number=2, repo="o/depth")
        probe = self.row(number=3, repo="o/probe")
        with patch.dict(CONFIG["limits"], {"max_open_prs_total": 1}), \
                patch.object(nextaction.plan, "now", return_value=self.clock):
            depth, = self.wave()["tasks"]
            self.assertEqual(depth["repo"], "o/depth")
            nextaction.acknowledge(depth["key"], "No suitable depth candidate")
            self.assertIsNone(self.wave())
            rows = read_json(self.ledger)
            next(row for row in rows if row["url"] == probe["url"])["status"] = "closed"
            write_json(self.ledger, rows)
            self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new"])

    def test_first_merge_pool_counts_only_recent_outside_open_prs_with_canonical_names(self):
        self.exploration = ["o/new"]
        self.row(repo="o/proven", status="merged")
        self.row(number=2, repo="O/OLD")
        self.row(number=3, repo=CONFIG["github_user"].upper() + "/own")
        self.row(number=4, repo="o/closed", status="closed")
        self.row(number=5, repo="o/old-open", submitted_at=(self.clock - dt.timedelta(days=181)).isoformat())
        with patch.dict(CONFIG["limits"], {"max_open_prs_total": 1}), \
                patch.dict(CONFIG, {"repo_renames": {"o/old": "o/proven"}}):
            self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new"])
            self.row(number=6, repo="o/boundary", submitted_at=(self.clock - dt.timedelta(days=180)).isoformat())
            self.assertIsNone(self.wave())

    def test_exploration_uses_configured_before_first_merge_cap(self):
        self.exploration = ["o/probe"]
        self.row(repo="o/probe")
        with patch.object(nextaction.plan, "now", return_value=self.clock):
            self.assertIsNone(self.wave())
            with patch.dict(CONFIG["limits"], {"max_open_prs_per_repo_before_first_merge": 2}):
                self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/probe"])
                self.row(number=2, repo="o/probe")
                self.assertIsNone(self.wave())

    def test_configured_three_hunt_budget_counts_empty_and_productive_hunts(self):
        self.lanes = ["o/one", "o/two", "o/three", "o/four"]
        configured = read_json(ROOT / "factory.config.json")["limits"]["max_hunts_per_shift"]
        self.assertEqual(configured, 3)
        with patch.dict(CONFIG["limits"], {"max_hunts_per_shift": configured}):
            first, second, third = self.wave()["tasks"]
            self.assertEqual([first["repo"], second["repo"], third["repo"]], self.lanes[:3])
            nextaction.acknowledge(first["key"], "No candidate")
            self.item("candidate", repo=second["repo"], created_at=self.clock.isoformat(), status="dropped")
            self.assertEqual(self.wave()["tasks"], [third])
            nextaction.acknowledge(third["key"], "No candidate")
            self.assertIsNone(self.wave())

    def test_default_hunt_budget_uses_three_worker_slots_without_overriding_explicit_limit(self):
        self.lanes = ["o/one", "o/two", "o/three", "o/four"]
        limits = {key: value for key, value in CONFIG["limits"].items() if key != "max_hunts_per_shift"}
        with patch.dict(CONFIG, {"limits": limits}):
            wave = self.wave()
            self.assertEqual(wave["concurrency"], 3)
            self.assertEqual(len(wave["tasks"]), 3)
        self.assertEqual(len(self.wave()["tasks"]), 2)

    def test_stalled_legacy_item_gets_triage_before_other_repositories_scout(self):
        item = self.item("stalled", "unreviewed", created_at=(self.clock - dt.timedelta(days=5)).isoformat())
        self.lanes = ["o/r", "o/other"]
        pending = self.actions()
        triage, = [entry for entry in pending if entry["kind"] == "stalled-item"]
        self.assertEqual(triage["target"], nextaction.item_target(item))
        self.assertLess(pending.index(triage), next(i for i, entry in enumerate(pending) if entry["kind"] == "wave"))
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/other"])
        for text in ("finish", "park", "dropped", "reason", "Do not bypass independent review"):
            self.assertIn(text, " ".join(triage["commands"]))
        with self.assertRaisesRegex(ValueError, "observed state"):
            nextaction.acknowledge(triage["id"], "Defer indefinitely")
        item.meta.update(status="parked", parked_reason="No longer wanted; wait for a maintainer invitation")
        item.save()
        self.assertEqual(self.actions("stalled-item"), [])
        self.assertIn("o/r", [task["repo"] for task in self.wave()["tasks"]])

    def test_real_plan_triages_stalled_missing_clone_without_restoring_it(self):
        item = self.item("stalled-missing", created_at=(self.clock - dt.timedelta(days=10)).isoformat())
        self.assertFalse(item.clone.exists())
        with patch.object(nextaction.plan, "run", side_effect=PLAN_RUN):
            triage, = self.actions("stalled-item")
            self.assertEqual(triage["target"], nextaction.item_target(item))
            self.assertIn("do not restore code merely to park", " ".join(triage["commands"]))
            self.assertEqual(self.actions("repair"), [])
            self.assertIsNone(self.wave())
            item.meta["updated_at"] = self.clock.isoformat()
            item.save()
            self.assertEqual(self.actions("stalled-item"), [])
            self.assertEqual(len(self.actions("repair")), 1)
        self.assertFalse(item.clone.exists())

    def test_stalled_schema_two_item_is_triaged_instead_of_repeated_implementation(self):
        item = self.item("stalled-build", "needs_implementation", schema=2,
                         created_at=(self.clock - dt.timedelta(days=10)).isoformat())
        self.assertEqual(len(self.actions("stalled-item")), 1)
        self.assertIsNone(self.wave())
        item.meta["updated_at"] = self.clock.isoformat()
        item.save()
        self.assertEqual(self.actions("stalled-item"), [])
        self.assertEqual(self.wave()["tasks"][0]["role"], "implement")

    def test_repeated_worker_starts_do_not_postpone_stalled_item_triage(self):
        old = self.clock - dt.timedelta(days=10)
        item = self.item("retry-only", schema=2, created_at=old.isoformat())
        self.demand(item)
        self.value(item)
        original_item = item.meta_path.read_bytes()

        def git(*args, at=None):
            env = dict(os.environ)
            if at:
                env.update(GIT_AUTHOR_DATE=at.isoformat(), GIT_COMMITTER_DATE=at.isoformat())
            subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.invalid", *args],
                           capture_output=True, text=True, check=True, env=env)

        def progress_git(argv, **kwargs):
            if "log" in argv or "status" in argv:
                return run(argv, **kwargs)
            return SimpleNamespace(stdout="", returncode=1 if "check-ignore" in argv else 0)

        git("init", "-b", "main")
        git("add", "runs")
        git("commit", "-m", "Initial work", at=old)
        with patch.object(nextaction, "run", side_effect=progress_git), \
                patch.object(nextaction.plan, "run", side_effect=PLAN_RUN), \
                patch.object(nextaction, "push_action", return_value=None), \
                patch.object(nextaction, "commit_action", return_value=None), \
                patch.object(nextaction, "release_action", return_value=None):
            self.assertEqual(len(self.actions("stalled-item")), 1)
            for _ in range(7):
                timeline.mark(item, "implement-started", head=HEAD)
                stamp = self.clock.timestamp()
                os.utime(item.file(timeline.FILE_NAME), (stamp, stamp))
                self.assertEqual(len(self.actions("stalled-item")), 1)
                self.assertIsNone(self.wave())
                git("add", "runs")
                git("commit", "-m", "Attempt history", at=self.clock)
                self.assertEqual(len(self.actions("stalled-item")), 1)
                self.assertIsNone(self.wave())
                self.assertEqual(item.meta_path.read_bytes(), original_item)
                self.clock += dt.timedelta(days=4)
            # Actual work evidence still reopens the item, dirty or committed.
            item.file("validation.md").write_text("Regression now passes; remaining checks pending.\n")
            stamp = self.clock.timestamp()
            os.utime(item.file("validation.md"), (stamp, stamp))
            self.assertEqual(self.actions("stalled-item"), [])
            self.assertEqual([task["role"] for task in self.wave()["tasks"]], ["implement"])
            git("add", "runs")
            git("commit", "-m", "Regression evidence", at=self.clock)
            self.assertEqual(self.actions("stalled-item"), [])
            self.clock += nextaction.ITEM_STALL
            self.assertEqual(len(self.actions("stalled-item")), 1)

    def test_waiting_item_stalls_but_submitted_and_inactive_items_do_not(self):
        for status in ("awaiting-maintainer", "parked", "blocked", "dropped", "merged"):
            self.item(status, status=status, created_at=(self.clock - dt.timedelta(days=10)).isoformat())
        self.item("submitted", status="submitted", pr_url="https://github.com/o/r/pull/1",
                  created_at=(self.clock - dt.timedelta(days=10)).isoformat())
        self.assertEqual([pathlib.Path(entry["target"]).name for entry in self.actions("stalled-item")], ["awaiting-maintainer"])

    def test_stall_uses_committed_or_dirty_evidence_not_checkout_mtimes(self):
        item = self.item("old", created_at=(self.clock - dt.timedelta(days=10)).isoformat())
        item.file("verify.md").write_text("Work evidence\n")
        recent = self.clock - dt.timedelta(days=1)
        old = self.clock - dt.timedelta(days=6)
        with patch.object(nextaction, "run", return_value=SimpleNamespace(stdout=recent.isoformat())), \
                patch.object(nextaction, "changed_paths", return_value=[]):
            self.assertIsNone(nextaction.stalled_action(item, self.clock))
        with patch.object(nextaction, "run", return_value=SimpleNamespace(stdout=old.isoformat())), \
                patch.object(nextaction, "changed_paths", return_value=[]):
            self.assertIsNotNone(nextaction.stalled_action(item, self.clock))
        os.utime(item.file("verify.md"), (recent.timestamp(), recent.timestamp()))
        changed = [(str(item.file("verify.md").relative_to(self.root)), False)]
        with patch.object(nextaction, "run", return_value=SimpleNamespace(stdout=old.isoformat())), \
                patch.object(nextaction, "changed_paths", return_value=changed):
            self.assertIsNone(nextaction.stalled_action(item, self.clock))

    def test_wave_cannot_be_acked_to_bypass_review(self):
        item = self.item("needs-review", "unreviewed")
        item.file("verify.md").write_text("verdict: ship\n")
        with self.assertRaisesRegex(ValueError, "observed state"):
            nextaction.acknowledge(self.actions("wave")[0]["id"], "skip")
        self.plan["unreviewed"] = []
        self.assertIsNone(self.wave())

    def test_feedback_moves_to_coordinator_then_existing_pr_submit_then_receipt(self):
        item = self.item("follow", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir.relative_to(self.root)))
        event = self.event("maintainer-review", row)
        task = self.wave()["tasks"][0]
        self.assertEqual(task["role"], "follow")
        self.assertIn(event["key"], task["note"])
        item.file("reply.md").write_text("Fixed the requested case.")
        write_json(item.file("follow.json"), {"event_keys": [event["key"]], "reply_file": "reply.md", "reply_only": True,
                                              "requests": [{"request": "case", "disposition": "accepted", "evidence": "receipt"}]})
        self.review(item, coordinator=True)
        self.review(item)
        stamp = item.file("review.md").stat().st_mtime - 10
        os.utime(item.file("coordinator-review.md"), (stamp, stamp))
        self.assertIsNone(self.wave())
        self.assertEqual(len(self.actions("coordinator-review")), 1)
        self.assertEqual(self.actions("ship"), [])
        self.review(item, coordinator=True)
        self.assertIn("bin/factory submit", " ".join(self.actions("ship")[0]["commands"]))
        item.meta["replies"] = [{"file": "reply.md", "url": row["url"] + "#comment", "at": self.clock.isoformat()}]
        item.save()
        self.assertIsNone(self.wave())
        self.assertEqual(self.actions("ship"), [])
        self.event("maintainer-review", row, key="second-comment")
        self.assertIsNotNone(self.wave())
        write_json(self.ledger, [{**row, "status": "merged", "merged_at": self.clock.isoformat()}])
        self.assertIsNone(self.wave())

    def test_recorded_follow_blocker_waits_without_repeating_or_stalling_other_work(self):
        item = self.item("blocked-follow", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir.relative_to(self.root)))
        event = self.event("conflict", row)
        saved = {"head": HEAD, "event_keys": [event["key"]], "blockers": ["Browser download timed out; zero tests ran"],
                 "reopen_when": "Pinned browser is installed; record the passing browser receipt"}
        write_json(item.file("follow.json"), saved)
        before = item.file("follow.json").read_bytes()
        self.assertIsNone(self.wave())
        report, = self.actions("report")
        self.assertIn("Not re-dispatched", report["why"])
        self.assertIn(saved["blockers"][0], report["why"])
        self.assertIn(saved["reopen_when"], report["why"])
        self.clock += dt.timedelta(hours=1)
        self.event("conflict", row, key=event["key"])
        self.assertIsNone(self.wave())
        self.assertEqual(self.actions("report")[0]["id"], report["id"])
        self.item("other", "stale_reviews", repo="o/other")
        self.assertEqual([task["role"] for task in self.wave()["tasks"]], ["review"])
        nextaction.acknowledge(report["id"], "Relayed unchanged")
        self.assertEqual(self.actions("report"), [])
        self.assertEqual(item.file("follow.json").read_bytes(), before)
        self.assertNotIn(item.dir, nextaction.code_items())

    def test_follow_blocker_preserves_owner_only_escalation_without_duplicate_actions(self):
        item = self.item("owner-prerequisite", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir.relative_to(self.root)))
        event = self.event("maintainer-comment", row)
        saved = {"head": HEAD, "event_keys": [event["key"]],
                 "blockers": ["Maintainer requires owner-authored certification"],
                 "reopen_when": "The owner supplies the required certification"}
        write_json(item.file("follow.json"), saved)
        self.assertIsNone(self.wave())
        report, = self.actions("report")
        self.assertIn(saved["reopen_when"], report["why"])
        self.assertNotIn("or create an owner action", report["why"])
        self.assertIn("Legal agreements, account/settings work and required owner-authored prose", report["why"])
        self.assertIn("docs/owner-actions.md", report["why"])
        self.assertIn("notify once", report["why"])
        self.assertIn("Do not duplicate owner actions or escalate ordinary environmental blockers", report["why"])
        self.clock += dt.timedelta(hours=1)
        self.event("maintainer-comment", row, key=event["key"])
        self.assertIsNone(self.wave())
        self.assertEqual(self.actions("report")[0]["id"], report["id"])

    def test_follow_blocker_reopens_only_for_changed_evidence(self):
        item = self.item("blocked-follow", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir.relative_to(self.root)))
        event = self.event("conflict", row)
        saved = {"head": HEAD, "event_keys": [event["key"]], "blockers": ["Browser missing"]}
        write_json(item.file("follow.json"), saved)
        self.assertIsNone(self.wave())
        with patch.object(Item, "head_sha", side_effect=CommandError("branch missing")):
            self.assertIn("could not check the blocked head", self.actions("repair")[0]["why"])
            self.assertIsNone(self.wave())
        item.meta["head"] = NEXT_HEAD
        item.save()
        self.assertEqual(self.wave()["tasks"][0]["role"], "follow")
        item.meta["head"] = HEAD
        item.save()
        self.assertIsNone(self.wave())
        write_json(item.file("follow.json"), {**saved, "blockers": [], "resolution": "Installed pinned browser; receipt 123"})
        self.assertEqual(self.wave()["tasks"][0]["role"], "follow")
        write_json(item.file("follow.json"), saved)
        self.assertIsNone(self.wave())
        self.event("maintainer-comment", row, key="new-feedback")
        self.assertEqual(self.wave()["tasks"][0]["role"], "follow")

    def test_legacy_blockers_without_head_do_not_trigger_a_retry_on_refresh(self):
        item = self.item("legacy-blocked", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir.relative_to(self.root)))
        event = self.event("conflict", row)
        write_json(item.file("follow.json"), {"event_keys": [event["key"]], "blockers": ["Browser missing"]})
        self.assertIsNone(self.wave())
        self.assertIn("clear its blockers list", self.actions("report")[0]["why"])
        write_json(item.file("follow.json"), {"event_keys": [event["key"]], "blockers": "invalid"})
        self.assertIn("blockers must be a list", self.actions("repair")[0]["why"])

    def test_saved_conflict_text_uses_the_current_history_preserving_instruction(self):
        item = self.item("conflict", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir.relative_to(self.root)))
        event = self.event("conflict", row, detail={"text": "CONFLICT: rebase the branch, rerun tests and gate"})
        before = self.ledger.with_name("followup-events.jsonl").read_bytes()
        task, = self.wave()["tasks"]
        self.assertIn(nextaction.followup.CONFLICT_REPAIR, task["note"])
        self.assertNotIn("rebase the branch", task["note"])
        self.assertIn(event["key"], task["note"])
        self.assertEqual(self.ledger.with_name("followup-events.jsonl").read_bytes(), before)

    def test_records_destinations_deferral_and_legacy_audit(self):
        row = self.row(status="merged", merged_at=self.clock.isoformat())
        records = self.actions("records")
        self.assertEqual([a["target"] for a in records], ["profile", "teach", "notebook"])
        self.assertNotIn("brief record", " ".join(c for a in records for c in a["commands"]))
        real = {"at": self.clock.isoformat(), "ref": "commit", "sha256": "1" * 64}
        receipts = {"profile": real, "teach": real, "notebook": {"at": self.clock.isoformat(), "deferred": "connector unavailable"}}
        write_json(self.ledger, [{**row, "records": receipts}])
        self.assertEqual(self.actions("records"), [])
        self.clock += dt.timedelta(hours=12)
        self.assertEqual([a["target"] for a in self.actions("records")], ["notebook"])
        receipts["notebook"] = real
        write_json(self.ledger, [{**row, "records": receipts}])
        self.assertEqual(self.actions("records"), [])
        write_json(self.ledger, [{**row, "recorded_at": self.clock.isoformat()}])
        self.assertEqual([a["target"] for a in self.actions("records")], ["audit"])
        write_json(self.ledger, [{**row, "records": {d: real for d in ("profile", "teach", "notebook")}}])
        self.assertEqual(self.actions("records"), [])

    def test_records_print_one_file_capture_command_per_destination(self):
        rows = [self.row(number=number, status="merged", merged_at=self.clock.isoformat()) for number in (1, 2)]
        actions = self.actions("records")
        profile = self.root / "repos/fixture-owner__fixture-owner/README.md"
        files = {"profile": str(profile), "teach": "docs/interview", "notebook": "<read-back note file>"}
        for entry in actions:
            dest = entry["target"]
            marks = [shlex.split(text) for text in entry["commands"] if text.startswith("bin/factory sync mark ")]
            captures = [args for args in marks if "--deferred" not in args]
            self.assertEqual(len(captures), 1, dest)
            expected = ["bin/factory", "sync", "mark", dest, "--file", files[dest]]
            if dest == "profile":
                expected += ["--ref", "<commit>"]
            self.assertEqual(captures[0], expected)
            deferrals = [args for args in marks if "--deferred" in args]
            self.assertEqual([args[4] for args in deferrals], [row["url"] for row in rows] if dest == "notebook" else [])

    def test_many_owed_life_os_records_print_one_deferral_template(self):
        for number in range(1, 8):
            self.row(number=number, status="merged", merged_at=self.clock.isoformat())
        notebook = next(entry for entry in self.actions("records") if entry["target"] == "notebook")
        deferrals = [text for text in notebook["commands"] if "--deferred" in text]
        self.assertEqual(len(deferrals), 1)
        self.assertIn("'<pr-url>'", deferrals[0])
        self.assertIn("each of the 7 PRs", deferrals[0])

    def test_empty_digest_does_not_close_records_actions(self):
        receipt = {"at": self.clock.isoformat(), "ref": "typed", "sha256": ""}
        self.row(status="merged", records={dest: receipt for dest in ("profile", "teach", "notebook")})
        self.assertEqual([entry["target"] for entry in self.actions("records")], ["profile", "teach", "notebook"])

    def test_teaching_brief_only_for_recorded_new_insight(self):
        item = self.item("merged", status="merged")
        self.row(status="merged", item=str(item.dir.relative_to(self.root)), merged_at=self.clock.isoformat())
        write_json(item.file("record.json"), {"insight": "A newly documented invariant"})
        teaching = next(a for a in self.actions("records") if a["target"] == "teach")
        self.assertIn("brief record --pr", " ".join(teaching["commands"]))

    def test_commit_selects_explicit_files_and_id_tracks_content(self):
        item = self.item("changed")
        item.file("proof.txt").write_text("first")
        self.status = " M runs/2026-11-10/changed/proof.txt\0?? docs/new.txt\0"
        entry = self.actions("commit")[0]
        self.assertEqual(entry["commands"][-1], "bin/factory commit -m '<what changed>' --path docs/new.txt --path runs/2026-11-10/changed/proof.txt")
        item.file("proof.txt").write_text("second")
        self.assertNotEqual(self.actions("commit")[0]["id"], entry["id"])
        self.status = ""
        self.assertEqual(self.actions("commit"), [])

    def test_commit_leaves_untracked_files_outside_items_and_factory_directories_alone(self):
        item = self.item("changed")
        item.file("new-proof.txt").write_text("proof")
        research = self.runs / "2026-11-10" / "research" / "dump.json"
        research.parent.mkdir(parents=True)
        research.write_text("{}")
        self.status = "?? native.d.ts\0?? runs/2026-11-10/research/dump.json\0"
        self.assertEqual(self.actions("commit"), [])
        self.status += "?? runs/2026-11-10/changed/new-proof.txt\0 M runs/2026-11-10/research/tracked.json\0?? docs/retro/2026-11-09.md\0"
        entry = self.actions("commit")[0]
        self.assertEqual(entry["commands"][-1], "bin/factory commit -m '<what changed>' --path docs/retro/2026-11-09.md "
                                                "--path runs/2026-11-10/changed/new-proof.txt --path runs/2026-11-10/research/tracked.json")
        self.assertIn("left alone: 2 ignored, oversized or unrelated untracked paths", entry["why"])
        self.assertNotIn("dump.json", entry["commands"][-1])
        before = entry["id"]
        research.write_text('{"changed": true}')
        self.assertEqual(self.actions("commit")[0]["id"], before)

    def test_commit_selects_shift_artifacts_and_changed_records_not_inherited_leftovers(self):
        item = self.item("inherited")
        item.file("proof.txt").write_text("previous shift\n")
        orphan = self.runs / "2026-11-10" / "previous" / "scout.json"
        orphan.parent.mkdir()
        orphan.write_text("{}")
        self.status = " M runs/2026-11-10/inherited/proof.txt\0?? runs/2026-11-10/previous/scout.json\0"
        code, text = self.run_cli("shift", "start", "--owner", "morning")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.actions("commit"), [])
        folder = self.runs / "2026-11-11" / "morning"
        folder.mkdir(parents=True)
        (folder / "summary.json").write_text("{}")
        (folder / "scout.txt").write_text("no candidate\n")
        item.file("current.txt").write_text("new proof\n")
        self.status += ("?? runs/2026-11-11/morning/summary.json\0?? runs/2026-11-11/morning/scout.txt\0"
                        "?? runs/2026-11-10/inherited/current.txt\0 M ledger/prs.json\0")
        entry = self.actions("commit")[0]
        self.assertEqual(entry["commands"][-1], "bin/factory commit -m '<what changed>' --path ledger/prs.json "
                         "--path runs/2026-11-10/inherited/current.txt --path runs/2026-11-11/morning/scout.txt "
                         "--path runs/2026-11-11/morning/summary.json")
        self.assertNotIn("previous/scout.json", entry["why"])
        self.assertNotIn("inherited/proof.txt", entry["why"])
        self.assertIn("records stay on main", entry["commands"][0])
        self.assertIn("do not create a shift branch", entry["commands"][0])
        self.assertIn("also pushes", entry["commands"][0])
        self.assertIn(nextaction.LOG_POLICY, entry["commands"])
        self.assertIn("do not commit them or rename them to .txt", "\n".join(entry["commands"]))
        state = shiftlock.load(self.root / shiftlock.STATE)
        shiftlock.run("renew", "morning", generation=state["generation"], ttl_hours=8)
        self.assertEqual(self.actions("commit")[0], entry)
        item.file("proof.txt").write_text("adopted and updated this shift\n")
        self.assertIn("--path runs/2026-11-10/inherited/proof.txt ", self.actions("commit")[0]["commands"][-1])
        self.status = "?? runs/2026-11-10/previous/scout.json\0"
        self.assertEqual(self.actions("commit"), [])

    def test_printed_commit_handles_staged_deletions_and_renames(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout

        git("init", "-q", "-b", "main")
        for change in ("staged-delete", "staged-rename", "unstaged-delete"):
            with self.subTest(change=change):
                item = self.item(change)
                old, new = item.file("old [proof].txt"), item.file("new [proof].txt")
                old.write_text("cited evidence\n")
                git("add", ".")
                git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "seed")
                if change == "staged-rename":
                    git("mv", str(old), str(new))
                elif change == "staged-delete":
                    git("rm", str(old))
                else:
                    old.unlink()
                with patch.object(nextaction, "run", run), patch.object(commit, "ROOT", self.root), \
                        patch.object(commit, "leaks", return_value=None) as scan, \
                        patch.object(publication, "run", side_effect=AssertionError("no publication")):
                    entry = nextaction.commit_action()
                    argv = shlex.split(entry["commands"][-1])
                    paths = [str(self.root / argv[i + 1]) for i, word in enumerate(argv) if word == "--path"]
                    expected_paths = [str(old)] + ([str(new)] if change == "staged-rename" else [])
                    self.assertCountEqual(paths, expected_paths)
                    result = commit.run_commit("save selected records", paths=paths, push=False)
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["commit"], git("rev-parse", "HEAD").strip())
                    expected = [f"D {old.relative_to(self.root)}"]
                    if change == "staged-rename":
                        expected.append(f"A {new.relative_to(self.root)}")
                        self.assertEqual(git("show", f"HEAD:{new.relative_to(self.root)}"), "cited evidence\n")
                    self.assertCountEqual(result["staged"], expected)
                    scan.assert_called_once()
                    self.assertEqual(git("status", "--porcelain"), "")
                    self.assertIsNone(nextaction.commit_action())

    def test_commit_paths_exclude_ignored_and_oversized_siblings_in_real_git(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout

        git("init", "-q", "-b", "main")
        folder = self.runs / "2026-11-10" / "morning"
        artifacts = folder / "artifacts" / "scout"
        artifacts.mkdir(parents=True)
        tracked_dump = artifacts / "tracked-api.json"
        tracked_dump.write_text("{}")
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "seed")
        (self.root / ".gitignore").write_text("runs/**/artifacts/\n")
        git("add", ".gitignore")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "ignore dumps")
        with patch.object(nextaction, "run", run):
            baseline, _ = nextaction.commit_changes(include_runs=True)
            shiftlock.run("acquire", "morning", commit_baseline=baseline)
            tracked_dump.write_text('{"new": true}')
            raw = artifacts / "tree.json"
            raw.write_bytes(b"x" * (nextaction.LIMIT + 1))
            oversized = folder / "large-listing.json"
            oversized.write_bytes(b"x" * (nextaction.LIMIT + 1))
            record = folder / "shift.json"
            record.write_text('{"evidence": "source URL and cited finding"}')
            entry = nextaction.commit_action()
            argv = shlex.split(entry["commands"][-1])
            paths = [argv[i + 1] for i, word in enumerate(argv) if word == "--path"]
            self.assertEqual(paths, [str(record.relative_to(self.root))])
            for excluded in (tracked_dump, raw, oversized):
                self.assertNotIn(excluded.name, json.dumps(entry))
            git("--literal-pathspecs", "add", "--", *paths)
            self.assertEqual(git("diff", "--cached", "--name-only").strip(), paths[0])
            self.assertIn("tracked-api.json", git("diff", "--name-only"))
            self.assertEqual(git("check-ignore", str(raw)).strip(), str(raw))
            self.assertTrue(oversized.exists())

    def test_many_untracked_paths_need_no_per_path_filesystem_access(self):
        (self.root / "tracked.txt").write_text("current record\n")
        self.status = " M tracked.txt\0?? native.d.ts\0" + "".join(f"?? stray-{n}.txt\0" for n in range(6000))
        resolve = pathlib.Path.resolve
        with patch.object(pathlib.Path, "resolve", autospec=True, side_effect=resolve) as resolved:
            entry = nextaction.commit_action()
        self.assertLess(resolved.call_count, 10)
        self.assertIn("left alone: 6001 ignored, oversized or unrelated untracked paths", entry["why"])
        self.assertIn("keep raw dumps local", entry["why"])
        self.assertLess(len(entry["why"]), 220)
        self.assertEqual(entry["commands"][-1], "bin/factory commit -m '<what changed>' --path tracked.txt")
        self.status = " M tracked.txt\0?? " + "a" * 300 + "\nname.txt\0"
        entry = nextaction.commit_action()
        self.assertNotIn("\n", entry["why"])
        self.assertLess(len(entry["why"]), 300)

    def test_measure_only_monday_and_until_recent_page(self):
        self.assertEqual(self.actions("measure"), [])
        self.clock = dt.datetime(2026, 11, 8, 16, tzinfo=dt.timezone.utc)
        entry = self.actions("measure")[0]
        self.assertIn("sync retro", " ".join(entry["commands"]))
        path = self.root / "docs/retro/2026-11-09.md"
        path.parent.mkdir(parents=True)
        path.write_text("# Weekly retrospective\n")
        self.assertEqual(self.actions("measure"), [])
        path.rename(path.with_name("2026-11-01.md"))
        self.assertEqual(len(self.actions("measure")), 1)

    def test_report_uses_utc8_boundary_and_open_owner_actions(self):
        self.clock = dt.datetime(2026, 11, 10, 16, 30, tzinfo=dt.timezone.utc)
        self.row(1, "merged", submitted_at="2026-11-10T15:59:59Z", merged_at="2026-11-10T16:00:00Z")
        self.row(2, "merged", submitted_at="2026-11-10T16:00:00Z", merged_at="2026-11-10T15:59:59Z")
        self.row(3, "submitted", submitted_at="2026-11-10T16:01:00Z")
        self.row(4, "awaiting-cla", submitted_at="2026-11-09T16:00:00Z")
        self.row(5, "closed", submitted_at="2026-11-10T16:00:00Z")
        self.row(6, "abandoned")
        path = self.root / "docs/owner-actions.md"
        path.parent.mkdir()
        path.write_text("## Open\n| Added | What |\n|---|---|\n| 2026-11-10 | Sign |\n| 2026-11-09 | ~~Done~~ |\n- [ ] Account setting\n- [x] Completed\n## Done\n| 2026-11-10 | Old |\n")
        sentence = "今日合入 1 个、今日提交 3 个、当前开着 2 个、待本人处理 2 项"
        self.assertEqual(nextaction.report_line(), sentence)
        report = self.actions("report")[0]
        self.assertEqual(report["title"], sentence)
        nextaction.acknowledge(report["id"], "Relayed unchanged")
        self.assertEqual(self.actions("report"), [])
        self.row(7, "submitted", submitted_at="2026-11-10T16:03:00Z")
        self.assertNotEqual(self.actions("report")[0]["id"], report["id"])
        self.clock += dt.timedelta(days=1)
        self.assertNotEqual(self.actions("report")[0]["id"], report["id"])

    def test_missing_lesson_precedes_an_unrelated_ship_and_closes_with_a_valid_record(self):
        self.item("ready", "ship_ready", repo="o/other")
        row = self.row(status="closed", lesson="L-missing")
        first = self.actions()[0]
        self.assertEqual((first["kind"], first["target"]), ("lesson", row["url"]))
        self.assertIn("bin/factory lesson add --pr", first["commands"][-1])
        with self.assertRaisesRegex(ValueError, "observed state"):
            nextaction.acknowledge(first["id"], "skip the missing record")
        with patch.object(nextaction.ledger, "lesson_ids", return_value={"L-present"}):
            write_json(self.ledger, [{**row, "lesson": "L-present"}])
            self.assertEqual(self.actions()[0]["kind"], "ship")
        write_json(self.ledger, [{**row, "status": "community-review", "silent": self.clock.isoformat()}])
        self.assertEqual(self.actions()[0]["kind"], "lesson")

    def test_action_order(self):
        self.disk.free = 4 * 1024 ** 3
        for bucket in ("needs_coordinator_review", "ship_ready", "stale_reviews"):
            self.review(self.item(bucket, bucket))
        self.row(status="merged", merged_at=self.clock.isoformat())
        self.status = " M tracked.txt\0"
        kinds = [a["kind"] for a in self.actions()]
        self.assertEqual(kinds, ["disk", "coordinator-review", "ship", "records", "records", "records", "wave", "commit", "report"])
        for entry in self.actions():
            self.assertEqual(set(entry), {"id", "kind", "role", "title", "why", "commands", "brief", "done_when", "target"})

    def test_read_only_projection_and_explicit_refresh(self):
        self.event("issue-confirmed")
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertIn("saved", self.actions("issue-confirmed")[0]["why"])
        nextaction.actions()
        after = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        stderr = io.StringIO()

        def sweep(progress):
            self.assertIn("checking PR observations", stderr.getvalue())
            progress("PR https://github.com/o/r/pull/1")

        def issues(progress):
            self.assertIn("checking waiting issues", stderr.getvalue())
            progress("Issue https://github.com/o/r/issues/2")

        with patch.object(nextaction.followup, "run", side_effect=sweep), \
                patch.object(nextaction.followup, "waiting_issues", side_effect=issues), redirect_stderr(stderr):
            code, stdout = self.run_cli("next", "--refresh", "--json", "--all")
        self.assertEqual(code, 0)
        self.assertIsInstance(json.loads(stdout), list)
        self.assertEqual(stderr.getvalue().splitlines(), ["refresh: checking PR observations", "refresh: PR https://github.com/o/r/pull/1",
                         "refresh: checking waiting issues", "refresh: Issue https://github.com/o/r/issues/2", "refresh: projecting next actions"])
        self.ledger.with_name("followup-events.jsonl").unlink()
        self.live_events = [{"invalid": "not a persisted event"}]
        self.assertEqual(nextaction.actions(), self.actions())

    def test_cli_first_all_json_idle_and_shift_lifecycle(self):
        _, text = self.run_cli("next")
        self.assertIn("Why:", text)
        self.assertIn("Done when:", text)
        code, text = self.run_cli("next", "--all", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(text)), 1)
        entry = self.actions("report")[0]
        self.run_cli("ack", entry["id"], "--note", "Relayed unchanged")
        self.assertEqual(self.run_cli("next"), (0, "idle\n"))
        self.assertEqual(self.run_cli("next", "--json"), (0, "null\n"))
        code, text = self.run_cli("shift", "start", "--owner", "morning")
        self.assertEqual(code, 0)
        self.assertIn("Generation:", text)
        state = read_json(self.root / shiftlock.STATE)
        for owner in ("morning", "evening"):
            code, text = self.run_cli("shift", "start", "--owner", owner)
            self.assertEqual(code, 1)
            self.assertIn("held by morning", text)
            self.assertIn("bin/factory next", text)
            self.assertEqual(read_json(self.root / shiftlock.STATE), state)
        self.assertEqual(self.run_cli("shift", "end", "--owner", "evening")[0], 1)
        code, text = self.run_cli("shift", "end", "--owner", "morning")
        self.assertEqual(code, 0)
        self.assertIn("今日合入", text)
        self.assertFalse((self.root / shiftlock.STATE).exists())

    def test_concurrent_same_owner_shift_start_has_one_winner(self):
        results = []
        args = SimpleNamespace(shift_command="start", owner="same", ttl_hours=4)
        with patch.object(nextaction, "print_action"), patch("builtins.print"):
            threads = [threading.Thread(target=lambda: results.append(nextaction.cmd_shift(args))) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(sorted(results), [0, 1])

    def test_broken_item_remains_a_repair_without_crashing_the_hunt_scan(self):
        item = self.item("broken")
        item.file("item.json").write_text("{")
        self.plan["errors"] = [{"item": str(item.dir.relative_to(self.root)), "error": "item.json is unreadable"}]
        self.assertEqual(len(self.actions("repair")), 1)

    def test_held_prebuild_item_does_not_need_a_readable_branch(self):
        item = self.item("prebuild", "held", schema=2)
        self.plan["held"][0]["reasons"] = ["value-review is required"]
        with patch.object(Item, "head_sha", side_effect=KeyError("branch")):
            self.assertIn("value-review", self.actions("held")[0]["why"])

    def read_mergers(self, response, repo="o/r"):
        followup = nextaction.followup
        with patch.object(followup, "_MERGERS", {}), patch.object(followup, "_EVENTS", []), \
                patch.object(followup, "iso", return_value=self.clock.isoformat()), \
                patch.object(followup, "gh_json", side_effect=[response]) as read:
            followup.repo_mergers(repo)
            read.assert_called_once()
            followup.persist_events()
            return list(followup._EVENTS)

    def test_merger_failure_defers_only_its_repository_and_returns_after_twelve_hours(self):
        self.read_mergers(RuntimeError("TLS timeout"))
        self.item("blocked", "ship_ready")
        self.item("other", "ship_ready", repo="o/other")
        self.plan["repo_slots"] = [{"repo": repo, "free": 1, "merged": 1} for repo in ("o/r", "o/scout")]
        self.lanes = ["o/r", "o/scout"]
        repair = self.actions()[0]
        self.assertTrue(repair["observation"])
        # Follow the printed refresh before acknowledging: it records another failure.
        self.read_mergers(RuntimeError("TLS timeout"))
        nextaction.acknowledge(repair["id"], "GraphQL still returns a TLS timeout; retry the repository read later")
        self.assertEqual(self.actions("repair"), [])
        self.assertEqual([a["target"] for a in self.actions("ship")], ["runs/2026-11-10/other"])
        self.assertEqual([t["repo"] for t in self.wave()["tasks"]], ["o/scout"])
        self.clock += dt.timedelta(hours=12)
        self.assertIn(repair["id"], [a["id"] for a in self.actions("repair")])
        nextaction.acknowledge(repair["id"], "The original read still needs a later retry")
        self.read_mergers(RuntimeError("new failure"))
        self.assertNotEqual(self.actions()[0]["id"], repair["id"])

    def test_actual_merger_read_recovers_inactive_repository_without_rewriting_history(self):
        followup = nextaction.followup
        self.read_mergers(RuntimeError("TLS timeout"))
        path = self.ledger.with_name("followup-events.jsonl")
        history = path.read_bytes()
        self.clock += dt.timedelta(minutes=1)
        with patch.object(followup, "_EVENTS", []), patch.object(followup, "_MERGERS", {}), \
                patch.object(followup, "iso", return_value=self.clock.isoformat()), \
                patch.object(followup, "gh_json", return_value={"data": {"repository": {"pullRequests": {"nodes": []}}}}), \
                patch("factory.util.global_hooks_drift", return_value=[]), \
                patch("factory.util.local_hooks_overrides", return_value=[]):
            FOLLOWUP_RUN()
        self.assertEqual(self.actions("repair"), [])
        self.assertTrue(path.read_bytes().startswith(history))

    def test_invalid_merger_responses_and_cache_hits_do_not_claim_recovery(self):
        for response in (None, {}, {"data": {"repository": None}},
                         {"errors": ["failed"], "data": {"repository": {"pullRequests": {"nodes": []}}}}):
            self.assertEqual([e["kind"] for e in self.read_mergers(response)], ["followup-error"])
        followup = nextaction.followup
        with patch.object(followup, "_MERGERS", {"o/r": frozenset()}), \
                patch.object(followup, "_EVENTS", []), patch.object(followup, "gh_json") as read:
            self.assertEqual(followup.repo_mergers("o/r"), frozenset())
            self.assertEqual(followup._EVENTS, [])
            read.assert_not_called()

    def test_successful_refresh_supersedes_a_saved_read_error(self):
        row = self.row()
        self.event("followup-error", row, {"text": "read timed out"})
        self.assertEqual(len(self.actions("repair")), 1)
        write_json(self.ledger, [{**row, "last_check": (self.clock + dt.timedelta(minutes=1)).isoformat()}])
        self.assertEqual(self.actions("repair"), [])

    def test_current_review_does_not_cover_new_follow_text(self):
        item = self.item("feedback", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir.relative_to(self.root)))
        event = self.event("maintainer-review", row)
        self.review(item)
        item.file("reply.md").write_text("A newly prepared answer")
        write_json(item.file("follow.json"), {"event_keys": [event["key"]], "reply_file": "reply.md",
                                              "requests": [{"request": "case", "disposition": "accepted", "evidence": "proof"}]})
        stamp = item.file("review.md").stat().st_mtime + 10
        os.utime(item.file("follow.json"), (stamp, stamp))
        self.assertEqual(self.wave()["tasks"][0]["role"], "follow")
        self.assertEqual(self.actions("coordinator-review"), [])

    def test_renewal_uses_the_active_owner_and_generation(self):
        self.item("stale", "stale_reviews")
        shiftlock.run("acquire", "morning", ttl_hours=4)
        holder = read_json(self.root / shiftlock.STATE)
        text = self.actions("wave")[0]["commands"][0]
        self.assertIn("--owner morning", text)
        self.assertIn("--generation " + holder["generation"], text)
        self.assertIn("--ttl-hours 8", text)

    def test_foreign_item_paths_rebase_to_configured_runs_including_nested_items(self):
        self.runs = self.root / "configured-items"
        self.runs.mkdir()
        with patch.dict(CONFIG, {"runs_dir": str(self.runs)}):
            for number, name in enumerate(("follow", "nested/merged"), 1):
                item = self.item(name, status="submitted" if number == 1 else "merged")
                foreign = self.root / "other-checkout/archive" / item.dir.relative_to(self.runs)
                foreign.mkdir(parents=True)
                foreign.joinpath("item.json").write_text("unreadable foreign metadata")
                self.assertEqual(nextaction.local_path(str(foreign)), item.dir)
                row = self.row(number, status="submitted" if number == 1 else "merged", item=str(foreign))
                if number == 1:
                    self.event("maintainer-review", row)
                    self.event("stale-pr-head", row)
                else:
                    write_json(item.file("record.json"), {"insight": "Shared invariant"})
            task = self.wave()["tasks"][0]
            self.assertEqual(task["item"], "configured-items/2026-11-10/follow")
            stale = self.actions("stale-head")[0]
            self.assertIn("bin/factory submit configured-items/2026-11-10/follow", " ".join(stale["commands"]))
            commands = " ".join(c for a in self.actions("records") for c in a["commands"])
            self.assertIn("brief record --pr", commands)
            self.assertEqual(self.actions("repair"), [])

    def test_missing_foreign_row_is_a_named_repair_and_other_actions_continue(self):
        absent = str(self.root / "other-checkout/runs/2026-11-10/absent")
        row = self.row(status="merged", item=absent)
        good = self.item("good", "stale_reviews")
        code, text = self.run_cli("next", "--all")
        self.assertEqual(code, 0)
        self.assertIn(row["url"], text)
        self.assertIn(absent, text)
        self.assertIn("Repair", text)
        self.assertEqual(self.wave()["tasks"][0]["item"], str(good.dir.relative_to(self.root)))
        self.assertIn("今日合入", text)

    def test_unreadable_evidence_is_a_repair_and_other_items_still_run(self):
        good = self.item("good", "stale_reviews")
        for number, name in enumerate(("review.json", "coordinator-review.json", timeline.FILE_NAME, "follow.json", "record.json", "item.json"), 1):
            with self.subTest(file=name):
                item = self.item(name.replace(".", "-"), "unreviewed", schema=2)
                if name == "follow.json":
                    item.meta["pr_url"] = self.row(number, item=str(item.dir))["url"]
                    item.save()
                    self.event("maintainer-review", read_json(self.ledger)[-1], key=name)
                elif name == "record.json":
                    self.row(number, status="merged", item=str(item.dir))
                item.file(name).write_text("{")
                actions = self.actions()
                repairs = [a for a in actions if a["kind"] == "repair" and item.dir.name in a["title"]]
                self.assertTrue(repairs)
                self.assertIn(name, repairs[0]["why"])
                self.assertEqual(self.wave()["tasks"][0]["item"], str(good.dir.relative_to(self.root)))
                self.assertNotIn(str(item.dir.relative_to(self.root)), [t.get("item") for t in self.wave()["tasks"]])

    def test_real_plan_errors_do_not_hide_other_item_actions(self):
        good = self.item("good")
        good.clone.mkdir(parents=True)
        good.file("verify.md").write_text("verdict: ship\n")
        for name in ("item.json", "value-review.json", "review.json", "coordinator-review.json"):
            item = self.item(name.replace(".", "-"), schema=2)
            self.demand(item)
            self.value(item)
            item.clone.mkdir(parents=True)
            item.file("verify.md").write_text("verdict: ship\n")
            item.file(name).write_text("{")
        with patch.object(nextaction.plan, "run", side_effect=PLAN_RUN), patch.object(Item, "merge_base", return_value=NEXT_HEAD):
            actions = self.actions()
            self.assertEqual(len([a for a in actions if a["kind"] == "repair"]), 3)
            held, = [a for a in actions if a["kind"] == "held"]
            self.assertEqual(held["why"], "Add a valid JSON object in value-review.json.")
            self.assertEqual(self.wave()["tasks"][0]["item"], str(good.dir.relative_to(self.root)))

    def test_real_plan_unreadable_legacy_review_does_not_hide_other_items(self):
        good = self.item("good")
        good.clone.mkdir(parents=True)
        good.file("verify.md").write_text("verdict: ship\n")
        damaged = self.item("damaged")
        damaged.clone.mkdir(parents=True)
        damaged.file("review.md").write_bytes(b"\xff")
        read_text = pathlib.Path.read_text
        for denied in (False, True):
            def read(path, *args, **kwargs):
                if denied and path == damaged.file("review.md"):
                    raise PermissionError("review is not readable")
                return read_text(path, *args, **kwargs)
            with self.subTest(denied=denied), patch.object(pathlib.Path, "read_text", read), \
                    patch.object(nextaction.plan, "run", side_effect=PLAN_RUN):
                repairs = self.actions("repair")
                self.assertEqual(len(repairs), 1)
                self.assertIn("review.md", repairs[0]["why"])
                self.assertEqual(pathlib.Path(self.wave()["tasks"][0]["item"]).name, "good")

    def test_invalid_metadata_fields_are_named_repairs_without_stopping_other_rows(self):
        self.item("good", "stale_reviews")
        for number, field in enumerate(("status", "pr_url", "repo", "schema"), 1):
            item = self.item(field)
            item.meta[field] = []
            item.save()
            self.row(number, item=str(item.dir))
        self.assertEqual(len(self.actions("repair")), 4)
        self.assertTrue(all("item.json" in action["why"] for action in self.actions("repair")))
        self.assertEqual(pathlib.Path(self.wave()["tasks"][0]["item"]).name, "good")

    def test_item_path_mapping_rejects_symlink_escape(self):
        external = self.root / "outside"
        external.mkdir()
        day = self.runs / "2026-11-10"
        day.mkdir()
        (day / "escape").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "no matching day/item tail"):
            nextaction.local_path(self.root / "other/runs/2026-11-10/escape")

    def test_schema_two_follow_reviews_bind_to_normalized_issue_text(self):
        item = self.item("follow", status="submitted", schema=2, issue_first="issue", issue=7,
                         pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir))
        event = self.event("maintainer-review", row)
        item.file("pr.md").write_text("Fix behavior\n\nCloses #7\n")
        item.file("reply.md").write_text("Fixed the requested case.")
        write_json(item.file("follow.json"), {"event_keys": [event["key"]], "reply_file": "reply.md",
                                              "requests": [{"request": "case", "disposition": "accepted", "evidence": "receipt"}]})
        result = {"head": HEAD, "verdict": "ship", "reviewer": "independent", "pr_sha256": verdicts.pr_text_sha256(item)}
        self.assertNotEqual(result["pr_sha256"], verdicts.pr_sha256(item))
        write_json(item.file("review.json"), result)
        write_json(item.file("coordinator-review.json"), result)
        self.assertEqual(len(self.actions("ship")), 1)
        self.assertIsNone(self.wave())
        item.file("pr.md").write_text("Fix behavior\n\nCloses #7\n\nChanged contract.")
        self.assertEqual(self.actions("ship"), [])
        self.assertEqual(self.wave()["tasks"][0]["role"], "follow")

    def test_unreadable_pr_text_does_not_abort_commit_or_other_items(self):
        item = self.item("unreadable", "stale_reviews")
        item.file("pr.md").write_text("PR text")
        self.item("good", "stale_reviews")
        self.status = " M runs/2026-11-10/unreadable/pr.md\0 M other.txt\0"
        read_bytes = pathlib.Path.read_bytes

        def read(path):
            if path == item.file("pr.md"):
                raise PermissionError(13, "Permission denied", str(path))
            return read_bytes(path)

        with patch.object(pathlib.Path, "read_bytes", read):
            actions = self.actions()
            self.assertTrue(any(a["kind"] == "repair" and "pr.md" in a["why"] for a in actions))
            self.assertEqual(pathlib.Path(self.wave()["tasks"][0]["item"]).name, "good")
            self.assertEqual(actions[-1]["kind"], "report")

    def test_plan_error_referenced_by_feedback_is_not_reloaded(self):
        item = self.item("damaged")
        item.file("item.json").write_text("[]")
        self.plan["errors"] = [{"item": str(item.dir.relative_to(self.root)), "error": "item.json must hold an object"}]
        self.event("maintainer-review", self.row(item=str(item.dir)))
        self.assertEqual(len(self.actions("repair")), 1)
        self.assertIsNone(self.wave())

    def test_wave_lock_keys_are_canonical_and_case_insensitive(self):
        self.item("old", "stale_reviews", repo="OLD/Kit")
        self.item("current", "stale_reviews", repo="NEW/Kit")
        self.lanes = ["O/Scout"]
        self.plan["repo_slots"] = [{"repo": "o/scout", "free": 1, "merged": 1}]
        with patch.dict(CONFIG, {"repo_renames": {"old/kit": "New/Kit"}}):
            self.assertEqual([t["lock_key"] for t in self.wave()["tasks"]], ["kit", "kit", "scout"])

    def test_incomplete_latest_review_requires_takeover_not_another_fix(self):
        item = self.item("incomplete", "fix_round", schema=2)
        write_json(item.file("review.json"), {"head": HEAD, "verdict": "fix-needed", "complete": False})
        self.assertEqual(len(self.actions("takeover")), 1)
        self.assertIsNone(self.wave())

    def test_late_completed_review_requires_takeover_not_another_fix(self):
        item = self.item("late", "fix_round", schema=2)
        write_json(item.file("review.json"), {"head": HEAD, "verdict": "fix-needed", "complete": True})
        write_json(item.file(timeline.FILE_NAME), [{"event": "review-started", "at": (self.clock - dt.timedelta(hours=3)).isoformat()}])
        os.utime(item.file("review.json"), (self.clock.timestamp(), self.clock.timestamp()))
        self.assertEqual(self.actions("takeover"), [])
        write_json(item.file(timeline.FILE_NAME), [{"event": "review-started", "head": HEAD,
                                                  "at": (self.clock - dt.timedelta(hours=3)).isoformat()}])
        self.assertEqual(len(self.actions("takeover")), 1)
        self.assertIsNone(self.wave())

    def test_schema_two_review_actions_name_the_exact_check(self):
        coordinator = self.item("coordinator", "needs_coordinator_review", schema=2)
        reviewer = self.item("reviewer", "stale_reviews", schema=2)
        takeover = self.item("takeover", "fix_round", schema=2)
        write_json(takeover.file("review.json"), {"head": HEAD, "verdict": "fix-needed"})
        write_json(takeover.file("review-round1.json"), {"head": HEAD, "verdict": "fix-needed"})
        self.assertIn(f"bin/factory review check --item {nextaction.item_target(coordinator)} coordinator",
                      self.actions("coordinator-review")[0]["done_when"])
        self.assertIn(f"bin/factory review check --item {nextaction.item_target(reviewer)} review",
                      self.actions("wave")[0]["done_when"])
        self.assertIn(f"bin/factory review check --item {nextaction.item_target(takeover)} review",
                      self.actions("takeover")[0]["done_when"])

    def test_schema_two_stale_head_repair_names_both_review_checks(self):
        item = self.item("stale-head", schema=2, pr_url="https://github.com/o/r/pull/1")
        self.event("stale-pr-head", self.row(item=str(item.dir)))
        done = self.actions("stale-head")[0]["done_when"]
        for part in ("review", "coordinator"):
            self.assertIn(f"bin/factory review check --item {nextaction.item_target(item)} {part}", done)

    def test_legacy_audit_names_clear_missing_and_all_sources(self):
        self.row(status="merged", recorded_at=self.clock.isoformat())
        audit = self.actions("records")[0]
        self.assertEqual(audit["target"], "audit")
        for flag in ("--clear-missing", "--readme", "--interview-dir", "--notebook-file"):
            self.assertIn(flag, audit["commands"][0])

    def test_lessons_routing_is_observable_ordered_and_bound_to_all_problems(self):
        self.lesson_problems = [f"L-{n:03d}: add an enforced-by route" for n in range(6)]
        self.status = " M lessons/LESSONS.md\0"
        actions = self.actions()
        self.assertEqual([a["kind"] for a in actions], ["route-lessons", "commit", "report"])
        routing = actions[0]
        self.assertEqual(routing["commands"], ["bin/factory lessons lint"])
        for problem in self.lesson_problems[:5]:
            self.assertIn(problem, routing["why"])
        self.assertNotIn(self.lesson_problems[5], routing["why"])
        with self.assertRaisesRegex(ValueError, "observed state"):
            nextaction.acknowledge(routing["id"], "skip")
        self.lesson_problems[5] = "L-005: correct the route"
        self.assertNotEqual(self.actions("route-lessons")[0]["id"], routing["id"])
        self.lesson_problems = []
        self.assertEqual(self.actions("route-lessons"), [])

    def test_manual_states_expiring_lease_limits_and_stays_short(self):
        for name in ("schedule/session-shift.md", "docs/repairs.md"):
            text = (ROOT / name).read_text()
            self.assertIn("lease", text)
            self.assertIn("lock acquire", text)
            self.assertIn("after expiry", text)
        manual = (ROOT / "schedule/session-shift.md").read_text()
        self.assertLessEqual(len(manual.splitlines()), 40)


if __name__ == "__main__":
    unittest.main()
