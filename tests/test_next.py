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
from factory.oss import profile_readme
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
        write_json(self.ledger.with_name("pr-history.json"), {"schema_version": 1, "prs": {}, "reconciliation": {"checked_at": self.clock.isoformat()}})
        self.header_projection = patch.object(nextaction, "profile_header_action", return_value=None)
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

        def local_run(argv, **kwargs):
            if argv[3:] == ["show", "refs/remotes/origin/main:README.md"]:
                return run(argv, **kwargs)
            return SimpleNamespace(returncode=1, stdout="") if "check-ignore" in argv else SimpleNamespace(stdout=self.status)

        patches = [self.header_projection,
                   patch.dict(CONFIG, {"runs_dir": str(self.runs), "ledger_file": str(self.ledger), "repos_dir": str(self.root / "repos"),
                                      "limits": {**CONFIG["limits"], "review_budget_minutes": 120}}),
                   patch.object(nextaction, "ROOT", self.root), patch.object(shiftlock, "ROOT", self.root),
                   patch.object(nextaction.clean, "ROOT", self.root),
                   patch.object(profile_readme, "ROOT", self.root),
                   patch.object(profile_readme, "gh_json", side_effect=AssertionError("no profile network lookup")),
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
                   patch.object(nextaction, "run", side_effect=local_run),
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
        self.plan["repo_slots"] = [{"repo": "o/r"}]
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
        self.plan["repo_slots"] = [{"repo": "o/r"}]
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

    def test_wave_selects_roles_slots_rounds_and_every_eligible_lane(self):
        for name, bucket, verdict in (("fix", "fix_round", "fix-needed"), ("stale", "stale_reviews", "ship")):
            self.review(self.item(name, bucket, repo="o/busy"), verdict=verdict)
        unreviewed = self.item("unreviewed", "unreviewed", repo="o/busy")
        unreviewed.file("verify.md").write_text("verdict: ship\n")
        self.item("unfinished", "unreviewed", repo="o/busy")
        repeated = self.item("repeated", "fix_round", repo="o/repeated")
        self.review(repeated, "fix-needed")
        repeated.file("review-round1.md").write_text(f"head: {HEAD}\nverdict: fix-needed\n")
        self.lanes = ["o/busy", "o/stopped", "o/full", "o/never", "o/one", "o/two", "o/three"]
        self.plan["repo_slots"] = [{"repo": r, "stopped": r == "o/stopped"}
                                   for r in self.lanes if r != "o/never"]
        wave = self.wave()
        self.assertEqual(wave["concurrency"], 3)
        self.assertEqual(wave["factory"], str(self.root))
        self.assertEqual([(t["role"], pathlib.Path(t["item"]).name) for t in wave["tasks"] if "item" in t],
                         [("fix", "fix"), ("review", "stale"), ("review", "unreviewed")])
        # Every eligible lane is scouted; only busy and stopped lanes stay out.
        self.assertEqual([t["repo"] for t in wave["tasks"] if t["role"] == "scout"], ["o/full", "o/never", "o/one", "o/two", "o/three"])
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

    def test_each_lane_is_hunted_once_per_shift_and_empty_scout_can_be_acked(self):
        self.lanes = ["o/one", "o/two", "o/three"]
        self.plan["repo_slots"] = [{"repo": r, "stopped": False} for r in self.lanes]
        first = self.wave()["tasks"]
        self.assertEqual([task["repo"] for task in first], self.lanes)
        nextaction.acknowledge(first[0]["key"], "No candidate: read all maintainer invitations; every issue is claimed")
        self.item("new", repo="o/two", created_at=self.clock.isoformat(), pr_url="https://github.com/o/two/pull/1")
        self.assertEqual(self.wave()["tasks"], first[2:])
        nextaction.acknowledge(first[2]["key"], "No candidate")
        self.assertIsNone(self.wave())
        self.clock += dt.timedelta(days=1)
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], self.lanes)

    def test_scouts_rotate_across_generations_by_last_scout_then_lane_order(self):
        self.lanes = ["o/one", "o/two", "o/three", "o/four", "o/five"]
        self.plan["repo_slots"] = [{"repo": r, "merged": 1} for r in self.lanes]
        first = self.wave()["tasks"]
        self.assertEqual([task["repo"] for task in first], self.lanes)
        for task in first[3:]:
            nextaction.acknowledge(task["key"], "No wanted unclaimed candidate")
        self.clock += dt.timedelta(hours=1)
        nextaction.acknowledge(first[0]["key"], "All examined requests are claimed")
        # A productive scout counts even when its candidate is later dropped.
        self.item("candidate", repo="o/two", created_at=self.clock.isoformat(), status="dropped")
        nextaction.acknowledge(first[2]["key"], "No wanted unclaimed candidate")
        self.assertIsNone(self.wave())
        saved = read_json(self.root / nextaction.ACKS)
        self.assertEqual(saved[first[0]["key"]]["repo"], "o/one")
        self.lanes.append("o/six")
        self.plan["repo_slots"].append({"repo": "o/six", "merged": 1})
        self.clock += dt.timedelta(days=1)
        # The never-scouted lane wins, then the oldest scouts, tied in lane order.
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/six", "o/four", "o/five", "o/one", "o/two", "o/three"])

    def test_empty_scout_waits_for_the_next_shift_with_no_cooldown(self):
        self.lanes = ["o/r"]
        with patch.dict(os.environ):
            os.environ.pop(shiftlock.RUN_STARTED, None)
            self.assertTrue(shiftlock.run("acquire", "first")["ok"])
            run_start = self.clock.isoformat()
            task, = self.wave()["tasks"]
            nextaction.acknowledge(task["key"], "No unclaimed candidate")
            self.assertIsNone(self.wave(), "the same shift does not scout a lane twice")
            self.assertTrue(shiftlock.run("release", "first")["ok"])
            self.clock += dt.timedelta(hours=1)
            self.assertTrue(shiftlock.run("acquire", "second")["ok"])
            with patch.dict(os.environ, {shiftlock.RUN_STARTED: run_start}):
                self.assertIsNone(self.wave(), "a re-taken lease in the same scheduled run is still the same shift")
            # The next shift scouts it again, with no upstream activity in between.
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
            # O/OLD's scout yesterday is o/current's, so the never-scouted lane goes first.
            self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/other", "o/current"])

    def test_active_exploration_is_not_blocked_by_open_pr_count_or_short_waits(self):
        self.exploration = ["o/probe", "o/waiting", "o/new"]
        for number in range(1, 6):
            self.row(number, repo="o/probe")
        self.row(number=6, repo="o/waiting", status="silent", submitted_at=(self.clock - dt.timedelta(days=4)).isoformat())
        with patch.object(nextaction.plan, "now", return_value=self.clock):
            self.assertEqual([task["repo"] for task in self.wave()["tasks"]], self.exploration)

    def test_real_active_exploration_waits_for_depth_then_takes_every_lane(self):
        def lane(repo):
            return {"repo": repo, "state": "active", "reason": "Fixture", "since": "2026-11-01"}
        path = self.root / "lines/oss/lanes.json"
        path.parent.mkdir(parents=True)
        write_json(path, {"depth": [lane("o/depth")], "explore": [lane("o/new1"), lane("o/new2")]})
        write_json(self.root / nextaction.ACKS, {
            "scout-prior": {"repo": "o/depth", "at": (self.clock - dt.timedelta(days=1)).isoformat(), "note": "No candidate"}})
        self.assertEqual(read_json(self.ledger), [])
        with patch.object(nextaction.lanes, "ROOT", self.root), patch.object(nextaction.lanes, "order", LANE_ORDER), \
                patch.object(nextaction.plan, "run", side_effect=PLAN_RUN):
            depth, = self.wave()["tasks"]
            self.assertEqual(depth["repo"], "o/depth")
            nextaction.acknowledge(depth["key"], "No suitable depth candidate")
            self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new1", "o/new2"])
            for task in self.wave()["tasks"]:
                nextaction.acknowledge(task["key"], "No suitable exploration candidate")
            self.assertIsNone(self.wave())
            self.clock += dt.timedelta(days=1)
            depth, = self.wave()["tasks"]
            self.assertEqual(depth["repo"], "o/depth")

    def test_exploration_requires_each_depth_lane_to_be_empty_in_this_shift(self):
        self.lanes = ["o/one", "o/two"]
        self.exploration = ["o/new"]
        first, second = self.wave()["tasks"]
        nextaction.acknowledge(first["key"], "No suitable candidate")
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/two"])
        nextaction.acknowledge(second["key"], "No suitable candidate")
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new"])
        self.clock += dt.timedelta(days=1)
        # Yesterday's empty hunts no longer count: the next shift scouts depth again first.
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/one", "o/two"])

    def test_productive_depth_hunt_unlocks_exploration_for_the_rest_of_the_shift(self):
        self.lanes = ["o/depth"]
        self.exploration = ["o/new1", "o/new2"]
        # Each lane is scouted once per shift, so a depth lane whose scout found a candidate is cleared too.
        self.item("candidate", repo="o/depth", created_at=self.clock.isoformat(), status="dropped")
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new1", "o/new2"])
        self.clock += dt.timedelta(days=1)
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/depth"])

    def test_depth_proposal_waiting_on_maintainers_does_not_hold_back_exploration(self):
        self.lanes = ["o/depth"]
        self.exploration = ["o/new"]
        self.item("proposal", repo="o/depth", created_at=(self.clock - dt.timedelta(days=2)).isoformat(),
                  status="awaiting-maintainer", proposal_url="https://github.com/o/depth/issues/1#issuecomment-1")
        # The waiting lane is not scouted again, and exploration follows.
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new"])
        self.item("building", repo="o/depth", created_at=(self.clock - dt.timedelta(days=2)).isoformat())
        self.assertIsNone(self.wave())

    def test_awaiting_status_without_a_posted_proposal_still_holds_back_exploration(self):
        self.lanes = ["o/depth"]
        self.exploration = ["o/new"]
        self.item("tracked", repo="o/depth", created_at=(self.clock - dt.timedelta(days=2)).isoformat(),
                  status="awaiting-maintainer", tracking_issue=7)
        self.assertIsNone(self.wave())

    def test_depth_work_started_this_shift_lets_exploration_run_but_blocks_the_next_shift(self):
        self.lanes = ["o/depth"]
        self.exploration = ["o/new"]
        self.item("building", repo="o/depth", created_at=self.clock.isoformat())
        # The depth lane is not scouted twice; exploration runs beside its implementation.
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new"])
        self.clock += dt.timedelta(days=1)
        self.assertIsNone(self.wave())

    def test_productive_exploration_hunt_counts_once_and_leaves_other_lanes(self):
        self.exploration = ["o/new1", "o/new2"]
        self.item("candidate", repo="o/new1", created_at=self.clock.isoformat(), status="dropped")
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new2"])

    def test_legacy_current_shift_empty_ack_unlocks_exploration(self):
        self.lanes = ["o/depth"]
        self.exploration = ["o/new1", "o/new2"]
        depth = self.wave()["tasks"][0]
        write_json(self.root / nextaction.ACKS, {depth["key"]: {"at": self.clock.isoformat(), "note": "No candidate"}})
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new1", "o/new2"])

    def test_unresponsive_depth_lane_unlocks_exploration_but_unfinished_work_does_not(self):
        self.lanes = ["o/depth"]
        self.exploration = ["o/new"]
        self.plan["repo_slots"] = [{"repo": "o/depth", "stopped": True}]
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new"])
        self.plan["repo_slots"] = []
        self.item("unfinished", repo="o/depth")
        self.assertIsNone(self.wave())

    def test_mixed_depth_conditions_unlock_exploration(self):
        self.lanes = ["o/full", "o/yesterday", "o/remaining"]
        self.exploration = ["o/new1", "o/new2"]
        self.plan["repo_slots"] = [{"repo": "o/full", "stopped": True}]
        write_json(self.root / nextaction.ACKS, {
            "scout-prior": {"repo": "o/yesterday", "at": (self.clock - dt.timedelta(days=1)).isoformat(), "note": "No candidate"}})
        depth = self.wave()["tasks"]
        self.assertEqual([task["repo"] for task in depth], ["o/remaining", "o/yesterday"])
        for task in depth:
            nextaction.acknowledge(task["key"], "No suitable depth candidate")
        exploration = self.wave()["tasks"]
        self.assertEqual([task["repo"] for task in exploration], ["o/new1", "o/new2"])
        for task in exploration:
            nextaction.acknowledge(task["key"], "No suitable exploration candidate")
        self.assertIsNone(self.wave())

    def test_productive_or_unresponsive_depth_unlocks_exploration(self):
        self.lanes = ["o/depth"]
        self.exploration = ["o/new"]
        self.item("candidate", repo="o/depth", created_at=self.clock.isoformat(), pr_url="https://github.com/o/depth/pull/1")
        self.row(repo="o/depth", status="merged")
        self.plan["repo_slots"] = [{"repo": "o/depth"}]
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new"])
        self.plan["repo_slots"][0]["stopped"] = True
        self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/new"])

    def test_config_has_no_scout_count_cap_and_each_lane_counts_once(self):
        limits = read_json(ROOT / "factory.config.json")["limits"]
        self.assertNotIn("max_hunts_per_shift", limits)
        self.assertNotIn("max_explore_per_shift", limits)
        self.lanes = ["o/one", "o/two", "o/three", "o/four", "o/five", "o/six"]
        tasks = self.wave()["tasks"]
        self.assertEqual([task["repo"] for task in tasks], self.lanes)
        nextaction.acknowledge(tasks[0]["key"], "No candidate")
        self.item("candidate", repo=tasks[1]["repo"], created_at=self.clock.isoformat(), status="dropped")
        self.assertEqual(self.wave()["tasks"], tasks[2:])
        for task in tasks[2:]:
            nextaction.acknowledge(task["key"], "No candidate")
        self.assertIsNone(self.wave())

    def test_every_active_exploration_lane_follows_cleared_depth(self):
        self.lanes = ["o/depth"]
        self.exploration = ["o/new1", "o/new2", "o/new3"]
        depth, = self.wave()["tasks"]
        nextaction.acknowledge(depth["key"], "No suitable depth candidate")
        exploration = self.wave()["tasks"]
        self.assertEqual([task["repo"] for task in exploration], self.exploration)
        nextaction.acknowledge(exploration[0]["key"], "No suitable exploration candidate")
        self.assertEqual(self.wave()["tasks"], exploration[1:])
        for task in exploration[1:]:
            nextaction.acknowledge(task["key"], "No suitable exploration candidate")
        self.assertIsNone(self.wave())

    def test_shift_target_does_not_hold_scouts_or_new_contribution_work(self):
        self.lanes = ["o/depth"]
        self.item("ready", "ship_ready")
        building = self.item("building", "needs_implementation", schema=2,
                             created_at=self.clock.isoformat(), updated_at=self.clock.isoformat())
        with patch.dict(CONFIG["limits"], {"new_prs_per_shift_target": 10}), \
                patch.dict(os.environ, {shiftlock.RUN_STARTED: self.clock.isoformat()}):
            for number in range(1, 13):
                self.row(number, submitted_at=self.clock.isoformat())
                if number not in (10, 12):
                    continue
                with self.subTest(opened=number):
                    pending = self.actions()
                    self.assertEqual(len([a for a in pending if a["kind"] == "ship"]), 1)
                    tasks = self.wave()["tasks"]
                    self.assertIn(("implement", nextaction.item_target(building)),
                                  [(task["role"], task.get("item")) for task in tasks])
                    self.assertEqual([task["repo"] for task in tasks if task["role"] == "scout"], ["o/depth"])
                    report, = [a for a in pending if a["kind"] == "report"]
                    self.assertIn(f"This shift has opened {number} new PRs toward its target of 10; the target is not a ceiling, "
                                  "and a shortfall is reported with its count and reason.", report["why"])

    def test_scout_wave_runs_one_watch_scan_per_shift_first(self):
        self.lanes = ["o/depth"]
        with patch.dict(os.environ, {shiftlock.RUN_STARTED: self.clock.isoformat()}):
            wave, = self.actions("wave")
            commands = wave["commands"]
            self.assertEqual(commands[1], "bin/factory watch")
            self.assertIn("newest runs/<date>/watch/ report", commands[2])
            self.assertLess(1, next(i for i, text in enumerate(commands) if text.startswith("Call Workflow")))
            folder = self.runs / self.clock.date().isoformat() / "watch"
            folder.mkdir(parents=True)
            write_json(folder / "before.json", {"at": (self.clock - dt.timedelta(minutes=1)).isoformat(), "dry_run": False})
            write_json(folder / "dry.json", {"at": self.clock.isoformat(), "dry_run": True})
            (folder / "broken.json").write_text("{")
            self.assertIn("bin/factory watch", self.actions("wave")[0]["commands"], "older, dry-run and unreadable scans do not count")
            failed = {"at": (self.clock + dt.timedelta(minutes=1)).isoformat(), "dry_run": False,
                      "repos": [{"repo": "o/depth", "status": "error", "error": "GitHub rate limit"}]}
            write_json(folder / "failed.json", failed)
            self.assertIn("bin/factory watch", self.actions("wave")[0]["commands"], "a failed scan runs again")
            write_json(folder / "after.json", {**failed, "repos": [{"repo": "o/depth", "status": "ok"}]})
            self.assertNotIn("bin/factory watch", self.actions("wave")[0]["commands"])

    def test_unresponsive_ledger_history_skips_only_that_repository_scout(self):
        self.lanes = ["o/silent", "o/active"]
        for number in (1, 2, 3):
            self.row(number, repo="o/silent", status="silent", lesson="Recorded response history",
                     submitted_at=(self.clock - dt.timedelta(days=40)).isoformat())
        self.row(4, repo="o/active", status="silent", lesson="Keep waiting",
                 submitted_at=(self.clock - dt.timedelta(days=40)).isoformat())
        with patch.object(nextaction.plan, "now", return_value=self.clock):
            self.assertEqual([task["repo"] for task in self.wave()["tasks"]], ["o/active"])

    def test_wave_lists_every_eligible_lane_and_keeps_three_worker_slots(self):
        self.lanes = ["o/one", "o/two", "o/three", "o/four"]
        wave = self.wave()
        self.assertEqual(wave["concurrency"], 3)
        self.assertEqual([task["repo"] for task in wave["tasks"]], self.lanes)

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

    def inline(self, row, comment_id, kind="community-inline", body="formatToParts aborts V8 for ksh"):
        return self.event(kind, row, key=f"{row['url']}#{kind}#{comment_id}",
                          detail={"text": f"COMMUNITY INLINE {row['url']} by bardrop", "body": body, "author_login": "bardrop",
                                  "comment_id": comment_id, "thread_comment_id": comment_id})

    def test_community_inline_feedback_is_followed_but_an_empty_review_summary_is_not(self):
        item = self.item("dp743", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir.relative_to(self.root)), last_check=self.clock.isoformat(), inline_pending=[11, 12])
        self.event("community-review", row, detail={"text": "COMMUNITY REVIEW COMMENTED by bardrop: ''", "body": "", "state": "COMMENTED"})
        self.assertIsNone(self.wave())
        first, second = self.inline(row, 11), self.inline(row, 12, body="divider becomes U+200F")
        task, = self.wave()["tasks"]
        self.assertEqual(task["role"], "follow")
        self.assertIn(first["key"], task["note"])
        self.assertIn(second["key"], task["note"])
        self.assertIn("divider becomes U+200F", task["note"])

    def test_inline_feedback_stays_owed_until_every_thread_reply_is_posted(self):
        item = self.item("dp743", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir.relative_to(self.root)), last_check=self.clock.isoformat(), inline_pending=[11, 12])
        events = [self.inline(row, 11), self.inline(row, 12)]
        for name in ("reply-1.md", "reply-2.md"):
            item.file(name).write_text("Fixed in the latest commit.")
        write_json(item.file("follow.json"), {"event_keys": [e["key"] for e in events],
                                              "replies": [{"file": "reply-1.md", "reply_to": 11, "destination": "inline"},
                                                          {"file": "reply-2.md", "reply_to": 12, "destination": "inline"}],
                                              "requests": [{"request": "crash", "disposition": "accepted", "evidence": "receipt"}]})
        self.clock += dt.timedelta(minutes=5)
        # A failed post records no URL; a restarted shift reads the same saved events and still owes both threads.
        item.meta["thread_replies"] = [{"file": "reply-1.md", "comment_id": 11, "url": "", "at": self.clock.isoformat()}]
        item.save()
        self.assertEqual(self.wave()["tasks"][0]["role"], "follow")
        item.meta["thread_replies"] = [{"file": "reply-1.md", "comment_id": 11, "url": row["url"] + "#r1", "at": self.clock.isoformat()}]
        item.save()
        self.assertEqual(self.wave()["tasks"][0]["role"], "follow")
        item.meta["thread_replies"].append({"file": "reply-2.md", "comment_id": 12, "url": row["url"] + "#r2", "at": self.clock.isoformat()})
        item.save()
        self.assertIsNone(self.wave())

    def test_settled_inline_thread_and_repeated_observation_do_not_redispatch(self):
        item = self.item("dp743", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir.relative_to(self.root)), last_check=self.clock.isoformat(), inline_pending=[11])
        event = self.inline(row, 11)
        self.live_events = [{**event, "at": (self.clock + dt.timedelta(hours=8)).isoformat()}]
        saved, _ = nextaction.observations(True)
        self.assertEqual([e["at"] for e in saved if e["key"] == event["key"]], [event["at"]])
        self.assertEqual(self.wave()["tasks"][0]["role"], "follow")
        write_json(self.ledger, [{**row, "inline_pending": [], "last_check": (self.clock + dt.timedelta(minutes=1)).isoformat()}])
        self.assertIsNone(self.wave())

    def test_external_status_is_classified_internally_and_never_dispatched_as_a_reply(self):
        # Owner, 2026-11-25: only problems a person or bot raised get a public reply; CI/deploy status does not.
        item = self.item("ctx7", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir.relative_to(self.root)), last_check=self.clock.isoformat())
        self.event("external-checks", row, detail={"text": "EXTERNAL CHECKS: Vercel"})
        self.event("bots", row, detail={"text": "BOTS: 1 comments from vercel[bot]", "body": "A member of the Team first needs to authorize it."})
        self.event("pre-existing-failures", row, detail={"text": "PRE-EXISTING FAILURES: osv-scan",
                                                          "jobs": [{"name": "osv-scan", "failing_on_base": True, "head_sha": HEAD}]})
        self.assertIsNone(self.wave())
        triage, = self.actions("ci-triage")
        self.assertIn("post no public status note", " ".join(triage["commands"]))
        self.assertIn("Reply only when a person or bot raises a problem", followup_rule := nextaction.followup.NO_STATUS_REPLY)
        self.assertIn(followup_rule, " ".join(triage["commands"]))

    def test_legacy_bot_comment_with_a_code_finding_is_recovered_but_notices_are_not(self):
        item = self.item("core", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir.relative_to(self.root)), last_check=self.clock.isoformat())
        self.event("bots", row, key=row["url"] + "#bots#900", detail={"text": "BOTS: 1 comments from changeset-bot",
                   "body": "### 🦋 Changeset detected\n\nLatest commit: abc"})
        self.event("bots", row, key=row["url"] + "#bots#901", detail={"text": "BOTS: 1 comments from vercel",
                   "body": "@factory-owner is attempting to deploy a commit. A member of the Team first needs to authorize it."})
        self.assertIsNone(self.wave())
        self.event("bots", row, key=row["url"] + "#bots#902", detail={"text": "BOTS: 1 comments from greptile-apps",
                   "body": "`src/cache.ts:42` returns the stale entry after `clear()`."})
        task, = self.wave()["tasks"]
        self.assertEqual(task["role"], "follow")
        self.assertIn(row["url"] + "#bot-comment#902", task["note"])
        self.assertNotIn("#bots#900", task["note"])
        self.assertEqual(nextaction.owed_feedback(read_json(self.ledger)[-1]), [row["url"] + "#bot-comment#902"])

    def test_queue_survives_restart_and_settles_only_after_the_reply_or_thread_resolution(self):
        item = self.item("core", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir.relative_to(self.root)), last_check=self.clock.isoformat(), bot_pending=[44])
        finding = self.event("bot-finding", row, key=row["url"] + "#bot-finding#44", detail={"text": "BOT FINDING", "body": "Missing await"})
        comment = self.event("bot-comment", row, key=row["url"] + "#bot-comment#45",
                             detail={"text": "BOT COMMENT", "body": "`src/a.ts:3` drops the error"})
        owed = sorted(nextaction.owed_feedback(read_json(self.ledger)[-1]))
        self.assertEqual(owed, sorted([finding["key"], comment["key"]]))
        # A restarted shift reads the same saved file; nothing was answered, so the same work is owed.
        self.assertEqual(sorted(nextaction.owed_feedback(read_json(self.ledger)[-1])), owed)
        self.assertEqual(self.wave()["tasks"][0]["role"], "follow")
        later = (self.clock + dt.timedelta(minutes=1)).isoformat()
        write_json(self.ledger, [{**row, "bot_pending": [], "last_check": later}])  # thread 44 resolved upstream
        self.assertEqual(nextaction.owed_feedback(read_json(self.ledger)[-1]), [comment["key"]])
        item.file("reply.md").write_text("Fixed in the latest commit.")
        write_json(item.file("follow.json"), {"event_keys": [comment["key"]], "reply_file": "reply.md",
                                              "requests": [{"request": "error", "disposition": "fixed", "evidence": "receipt"}]})
        item.meta["replies"] = [{"file": "reply.md", "url": "", "at": later}]  # a failed post
        item.save()
        self.assertEqual(nextaction.owed_feedback(read_json(self.ledger)[-1]), [comment["key"]])
        item.meta["replies"] = [{"file": "reply.md", "url": row["url"] + "#issuecomment-9", "at": later}]
        item.save()
        self.assertEqual(nextaction.owed_feedback(read_json(self.ledger)[-1]), [])
        self.assertIsNone(self.wave())

    def test_ci_failure_is_owed_until_triaged_and_a_conflict_settles_once_mergeable(self):
        row = self.row(last_check=self.clock.isoformat(), checks={"failing": ["osv-scan"]}, mergeable="CONFLICTING")
        self.event("ci-failing", row, key=row["url"] + "#ci-failing#1",
                   detail={"text": "CI FAILING", "head_sha": HEAD, "jobs": [{"name": "osv-scan", "run_id": 1, "head_sha": HEAD}]})
        conflict = self.event("conflict", row, key=row["url"] + "#conflict#" + HEAD)
        owed, = nextaction.owed_ci(read_json(self.ledger)[-1])
        triage, = self.actions("ci-triage")
        self.assertEqual(owed, triage["id"])
        nextaction.acknowledge(triage["id"], "osv-scan: external — lockfiles untouched by the PR; main's scan passes")
        self.assertEqual(nextaction.owed_ci(read_json(self.ledger)[-1]), [])
        self.assertEqual(nextaction.owed_feedback(read_json(self.ledger)[-1]), [conflict["key"]])
        write_json(self.ledger, [{**row, "mergeable": "MERGEABLE", "last_check": (self.clock + dt.timedelta(minutes=1)).isoformat()}])
        self.assertEqual(nextaction.owed_feedback(read_json(self.ledger)[-1]), [])

    def test_bot_feedback_end_to_end_from_saved_events_to_a_posted_reply(self):
        item = self.item("core", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir.relative_to(self.root)), last_check=self.clock.isoformat())
        bodies = {"901": "`src/cache.ts` returns the stale entry after `clear()`; reset `size` too.",
                  "902": "Could you add a regression test for an empty stop sequence?",
                  "903": "Reviewed `src/cache.ts:42`. LGTM, no issues found.",
                  "904": "<details><summary>Outside diff range comments (0)</summary>No issues found.</details>",
                  "905": "Ran the docs build for this change: `docs/api.md` updated."}
        for number, body in bodies.items():  # saved before bot-comment existed, as the live ledger holds them
            self.event("bots", row, key=f"{row['url']}#bots#{number}", detail={"text": "BOTS", "body": body})
        task, = self.wave()["tasks"]
        self.assertEqual(task["role"], "follow")
        for number in ("901", "902"):
            self.assertIn(f"#bot-comment#{number}", task["note"])
        for number in ("903", "904", "905"):
            self.assertNotIn(f"#{number}\"", task["note"])
        triage, = self.actions("bot-triage")
        self.assertIn("Never post a status", " ".join(triage["commands"]))
        current = read_json(self.ledger)[-1]
        owed = sorted(nextaction.owed_feedback(current))
        self.assertEqual(owed, [f"{row['url']}#bot-comment#901", f"{row['url']}#bot-comment#902"])
        self.assertEqual(nextaction.owed_triage(current), [triage["id"]])
        # Restart: the same saved file yields the same work.
        self.assertEqual(sorted(nextaction.owed_feedback(current)), owed)
        nextaction.acknowledge(triage["id"], "no finding — a docs build notice; nothing to answer")
        self.assertEqual(nextaction.owed_triage(current), [])
        item.file("reply.md").write_text("Fixed and covered by a regression test.")
        write_json(item.file("follow.json"), {"event_keys": owed, "reply_file": "reply.md",
                                              "requests": [{"request": "stale entry", "disposition": "fixed", "evidence": "receipt"}]})
        later = (self.clock + dt.timedelta(minutes=1)).isoformat()
        item.meta["replies"] = [{"file": "reply.md", "url": "", "at": later}]  # the post failed
        item.save()
        self.assertEqual(sorted(nextaction.owed_feedback(current)), owed)
        item.meta["replies"] = [{"file": "reply.md", "url": row["url"] + "#issuecomment-1", "at": later}]
        item.save()
        self.assertEqual(nextaction.owed_feedback(current), [])
        self.assertIsNone(self.wave())

    def test_open_pr_feedback_is_dispatched_before_new_work(self):
        self.item("needs-review", "stale_reviews", repo="o/other")
        item = self.item("dp743", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(item.dir.relative_to(self.root)), last_check=self.clock.isoformat(), inline_pending=[11])
        self.inline(row, 11)
        self.lanes = ["o/third"]
        self.assertEqual([t["role"] for t in self.wave()["tasks"]][:2], ["follow", "review"])

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
        self.assertEqual([a["target"] for a in self.actions("records")], ["notebook", "history"])
        write_json(self.ledger.with_name("pr-history.json"), {"schema_version": 1, "prs": {}, "reconciliation": {"checked_at": self.clock.isoformat()}})
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

    def test_history_refresh_when_never_attempted_or_stale(self):
        path = self.ledger.with_name("pr-history.json")
        for attempt in (None, self.clock - dt.timedelta(hours=13)):
            with self.subTest(attempt=attempt):
                if attempt is None:
                    path.unlink()
                else:
                    write_json(path, {"schema_version": 1, "prs": {}, "reconciliation": {"checked_at": attempt.isoformat()}})
                before = path.read_bytes() if path.exists() else None
                with patch.object(nextaction, "run", side_effect=AssertionError("no subprocess for history projection")):
                    entry = nextaction.history_action(self.clock)
                stamp = attempt.isoformat() if attempt else None
                self.assertEqual(entry["id"], nextaction.identifier("records", "history", stamp))
                self.assertEqual((entry["kind"], entry["target"], entry["role"]), ("records", "history", "coordinator"))
                self.assertEqual(entry["title"], "Refresh saved pull request history")
                self.assertTrue(entry["observation"])
                self.assertIn(stamp or "never", entry["why"])
                self.assertIn("public counts, daily activity and the profile header", entry["why"])
                self.assertEqual(entry["commands"], [
                    "bin/factory metrics history --cache ledger/pr-history.json",
                    "bin/factory commit -m 'Refresh saved pull request history' --path ledger/pr-history.json",
                    "Exit 3 from the refresh means GitHub could not be read completely: the cache keeps the last successful counts and records the attempt, so commit it either way."])
                self.assertEqual(entry["done_when"], "The saved GitHub observation was attempted within the last twelve hours.")
                self.assertEqual(before, path.read_bytes() if path.exists() else None)
                self.assertEqual(self.actions("records"), [entry])

    def test_history_invalid_attempt_time_counts_as_never_attempted(self):
        path = self.ledger.with_name("pr-history.json")
        cases = [{}, {"reconciliation": None}, {"reconciliation": []}]
        cases += [{"reconciliation": {"checked_at": stamp}} for stamp in (None, 123, "invalid", "2026-11-10", "2026-11-10T17:00:00")]
        for extra in cases:
            with self.subTest(extra=extra):
                write_json(path, {"schema_version": 1, "prs": {}, **extra})
                before = path.read_bytes()
                self.assertEqual(nextaction.history_action(self.clock)["id"], nextaction.identifier("records", "history", None))
                self.assertEqual(path.read_bytes(), before)

    def test_unreadable_history_cache_is_quarantined_and_restored_before_refresh(self):
        self.profile()
        path = self.ledger.with_name("pr-history.json")
        stamp = self.clock.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for content in (b"{", b"\xff", b"null", b"[]", b"{}", b'{"schema_version": 2, "prs": {}}', b'{"schema_version": 1, "prs": []}'):
            with self.subTest(content=content):
                path.write_bytes(content)
                with patch.object(profile_readme, "portfolio", side_effect=AssertionError("header waits for a readable history")):
                    entry, = self.actions("repair")
                    self.assertEqual(self.actions("records"), [])
                self.assertEqual(entry["id"], nextaction.identifier("repair", "history-cache", ["unreadable", "ledger/pr-history.json"]))
                self.assertEqual(entry["title"], "Repair the saved pull request history")
                copy = self.root / f"workspace/quarantine/pr-history-{stamp}.json"
                self.assertEqual(entry["commands"][:3], [
                    shlex.join(["mkdir", "-p", str(copy.parent)]),
                    shlex.join(["cp", "ledger/pr-history.json", str(copy)]),
                    "git restore --source=HEAD -- ledger/pr-history.json"])
                self.assertEqual(path.read_bytes(), content)
        path.unlink()
        self.assertEqual([entry["target"] for entry in self.actions("records")], ["history"])
        self.assertEqual(self.actions("repair"), [])

    def test_history_outside_the_ledger_directory_asks_for_a_location_repair(self):
        ledger = self.root / "data/prs.json"
        ledger.parent.mkdir()
        write_json(ledger, [])
        with patch.dict(CONFIG, {"ledger_file": str(ledger)}):
            entry = nextaction.history_action(self.clock)
        self.assertEqual((entry["kind"], entry["target"]), ("repair", "history-cache"))
        self.assertEqual(entry["title"], "Repair the saved history location")
        self.assertIn(str(ledger.with_name("pr-history.json")), entry["why"])
        self.assertNotIn("bin/factory metrics history", " ".join(entry["commands"]))

    def test_history_timestamp_overflow_keeps_pending_ci_in_cli(self):
        path = self.ledger.with_name("pr-history.json")
        self.event("ci-failing", detail={"jobs": [{"name": "test"}]})
        for stamp in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"):
            with self.subTest(stamp=stamp):
                write_json(path, {"schema_version": 1, "prs": {}, "reconciliation": {"checked_at": stamp}})
                before = path.read_bytes()
                code, output = self.run_cli("next", "--all", "--json")
                self.assertEqual(code, 0)
                entries = json.loads(output)
                self.assertIn("ci-triage", [entry["kind"] for entry in entries])
                history, = [entry for entry in entries if entry["target"] == "history"]
                self.assertEqual(history["id"], nextaction.identifier("records", "history", None))
                self.assertEqual(path.read_bytes(), before)

    def test_history_recent_attempts_include_failures_and_use_exact_timestamps(self):
        path = self.ledger.with_name("pr-history.json")
        for hours, error in ((11, None), (1, "transport_error")):
            with self.subTest(hours=hours, error=error):
                attempt = self.clock - dt.timedelta(hours=hours)
                check = {"checked_at": attempt.astimezone(nextaction.ZONE).isoformat(),
                         "activity": {"checked_at": (self.clock - dt.timedelta(days=2)).isoformat()}}
                if error:
                    check["error"] = error
                write_json(path, {"schema_version": 1, "prs": {}, "reconciliation": check})
                self.assertIsNone(nextaction.history_action(self.clock))
                self.assertEqual(self.actions("records"), [])
        boundary = self.clock - dt.timedelta(hours=12)
        write_json(path, {"schema_version": 1, "prs": {}, "reconciliation": {"checked_at": (boundary + dt.timedelta(microseconds=1)).isoformat()}})
        self.assertIsNone(nextaction.history_action(self.clock))
        write_json(path, {"schema_version": 1, "prs": {}, "reconciliation": {"checked_at": boundary.isoformat()}})
        self.assertIsNotNone(nextaction.history_action(self.clock))

    def test_history_id_changes_only_with_attempt_time(self):
        path = self.ledger.with_name("pr-history.json")
        attempt = self.clock - dt.timedelta(hours=13)
        check = {"checked_at": attempt.isoformat()}
        write_json(path, {"schema_version": 1, "prs": {}, "reconciliation": check})
        first = nextaction.history_action(self.clock)
        self.clock += dt.timedelta(hours=1)
        self.assertEqual(nextaction.history_action(self.clock)["id"], first["id"])
        write_json(path, {"schema_version": 1, "prs": {}, "reconciliation": {**check, "error": "transport_error"}})
        self.assertEqual(nextaction.history_action(self.clock)["id"], first["id"])
        write_json(path, {"schema_version": 1, "prs": {}, "reconciliation": {"checked_at": (attempt + dt.timedelta(microseconds=1)).isoformat()}})
        second = nextaction.history_action(self.clock)
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(nextaction.history_action(self.clock)["id"], second["id"])

    def test_history_deferral_lasts_twelve_hours_without_unlocking_header(self):
        self.profile()
        self.ledger.with_name("pr-history.json").unlink()
        entry, = self.actions("records")
        self.assertEqual(entry["target"], "history")
        nextaction.acknowledge(entry["id"], "Refresh cannot start while the cache destination is read-only")
        self.clock += dt.timedelta(hours=11)
        self.assertEqual(self.actions("records"), [])
        self.clock += dt.timedelta(hours=1)
        self.assertEqual(self.actions("records"), [entry])

    def profile_git(self, profile, *args):
        return run(["git", "-C", str(profile.parent), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", *args]).stdout.strip()

    def save_profile(self, profile, published=True):
        self.profile_git(profile, "add", "--", "README.md")
        self.profile_git(profile, "commit", "--allow-empty", "-m", "Update profile fixture")
        head = self.profile_git(profile, "rev-parse", "HEAD")
        if published:
            self.profile_git(profile, "update-ref", "refs/remotes/origin/main", head)
        return head

    def profile(self):
        self.header_projection.stop()
        path = pathlib.Path(CONFIG["repos_dir"]) / (CONFIG["github_user"] + "__" + CONFIG["github_user"]) / "README.md"
        path.parent.mkdir(parents=True)
        self.profile_git(path, "init", "-b", "main")
        path.write_text("# Manual\n" + profile_readme.PORTFOLIO_START + "\nstale header\n" + profile_readme.PORTFOLIO_END + "\n")
        self.save_profile(path)
        settings = self.root / "lines/oss/profile-highlights.json"
        settings.parent.mkdir(parents=True)
        write_json(settings, {"active_contributor": {"minimum_merged": 5, "limit": 6}, "selected": []})
        write_json(self.ledger.with_name("pr-history.json"), {
            "schema_version": 1, "prs": {}, "reconciliation": {"checked_at": self.clock.isoformat()}})
        return path

    def test_profile_header_mismatch_prints_one_refresh_without_privacy_config(self):
        with patch.dict(CONFIG, {**CONFIG, "repos_dir": str(self.root / "repos with spaces")}, clear=True):
            del CONFIG["private_terms"]
            del CONFIG["git_email_previous"]
            profile = self.profile()
            before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
            with patch.object(profile_readme, "check", side_effect=AssertionError("no privacy check")):
                entry, = self.actions("records")
            self.assertEqual((entry["kind"], entry["target"], entry["role"]), ("records", "profile-header", "record"))
            self.assertEqual(entry["commands"], [
                f"git -C {shlex.quote(str(profile.parent))} pull --ff-only",
                f"bin/factory sync profile --readme {shlex.quote(str(profile))} --write",
                f"bin/factory profile-check --readme {shlex.quote(str(profile))}",
                'Scan, commit and push the profile repository as docs/repairs.md#profile-repository describes with commit message "Refresh profile header"; verify the remote commit.'])
            self.assertEqual(entry["done_when"], "The profile checkout's origin/main README carries the generator's header for the current retained snapshot.")
            self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_profile_header_closes_only_after_origin_main_moves(self):
        profile = self.profile()
        first, = self.actions("records")
        text = profile_readme.render_readme(profile.read_text(), [], {"groups": []}, CONFIG["github_user"])
        profile.write_text(text)
        self.assertEqual(self.actions("records"), [first])
        head = self.save_profile(profile, published=False)
        self.assertEqual(self.actions("records"), [first])
        self.profile_git(profile, "update-ref", "refs/remotes/origin/main", head)
        self.assertEqual(self.actions("records"), [])
        self.clock += dt.timedelta(hours=11)
        self.assertEqual(self.actions("records"), [])
        self.clock += dt.timedelta(hours=13)
        self.assertIsNone(nextaction.profile_header_action(profile))
        self.assertEqual([entry["target"] for entry in self.actions("records")], ["history"])
        write_json(self.ledger.with_name("pr-history.json"), {
            "schema_version": 1, "prs": {}, "reconciliation": {"checked_at": self.clock.isoformat()}})
        entry, = self.actions("records")
        self.assertEqual(entry["target"], "profile-header")
        self.assertNotEqual(entry["id"], first["id"])

    def test_profile_header_comparison_preserves_whitespace_like_profile_check(self):
        profile = self.profile()
        intro = "\n" + profile_readme.portfolio([], CONFIG["github_user"])
        with patch.object(profile_readme, "load_groups", return_value={"groups": []}):
            for content in (intro, intro.lstrip("\n"), intro + "\n", intro + " "):
                with self.subTest(content=content[-20:]):
                    text = "# Manual\n" + profile_readme.PORTFOLIO_START + content + profile_readme.PORTFOLIO_END + "\n"
                    profile.write_text(text)
                    self.save_profile(profile)
                    expected = bool(profile_readme.check(text, CONFIG["github_user"])["portfolio_mismatch"])
                    self.assertEqual(bool(self.actions("records")), expected)

    def test_profile_merge_receipts_suppress_a_separate_header_refresh(self):
        self.profile()
        self.row(status="merged", merged_at=self.clock.isoformat())
        with patch.object(profile_readme, "portfolio", side_effect=AssertionError("merge action already rewrites header")):
            self.assertEqual([entry["target"] for entry in self.actions("records")], ["profile", "teach", "notebook"])

    def test_profile_header_refresh_ignores_missing_or_malformed_published_markers(self):
        profile = self.profile()
        start, end = profile_readme.PORTFOLIO_START, profile_readme.PORTFOLIO_END
        for text in ("# Manual\n", start, end, end + start, start + start + end):
            with self.subTest(text=text), patch.object(profile_readme, "portfolio", side_effect=AssertionError("no valid header")):
                profile.write_text(text)
                self.save_profile(profile)
                self.assertEqual(self.actions("records"), [])
                self.assertEqual(self.actions("repair"), [])

    def test_missing_profile_checkout_prints_restore_command(self):
        self.header_projection.stop()
        profile = pathlib.Path(CONFIG["repos_dir"]) / (CONFIG["github_user"] + "__" + CONFIG["github_user"]) / "README.md"
        with patch.object(nextaction, "run", side_effect=AssertionError("restoration runs only when requested")):
            entry = nextaction.profile_header_action(profile)
        self.assertEqual(entry["id"], nextaction.identifier("records", "profile-header", "checkout"))
        self.assertEqual(entry["title"], "Restore the profile checkout")
        self.assertEqual(entry["commands"], [f"bin/factory sync profile --readme {shlex.quote(str(profile))}"])
        self.assertEqual(entry["done_when"], "The profile checkout exists with an origin/main README.")
        self.assertEqual(self.actions("records"), [entry])
        profile.parent.mkdir(parents=True)
        profile.write_text("A directory without git metadata\n")
        repair, = self.actions("repair")
        self.assertEqual(self.actions("records"), [])
        self.assertEqual(repair["id"], nextaction.identifier("repair", "profile-checkout", str(profile.parent)))
        stamp = self.clock.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        moved = self.root / f"workspace/quarantine/{profile.parent.name}-{stamp}"
        self.assertEqual(repair["commands"], [shlex.join(["mkdir", "-p", str(moved.parent)]), shlex.join(["mv", str(profile.parent), str(moved)]),
                                              f"bin/factory sync profile --readme {shlex.quote(str(profile))}"])
        self.assertTrue(profile.exists())

    def test_missing_working_readme_does_not_hide_the_published_header(self):
        profile = self.profile()
        entry, = self.actions("records")
        profile.unlink()
        self.assertEqual(self.actions("records"), [entry])
        self.profile_git(profile, "add", "--", "README.md")
        self.profile_git(profile, "commit", "-m", "Remove profile fixture")
        self.profile_git(profile, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertEqual(self.actions("records"), [])
        self.assertEqual(self.actions("repair"), [])

    def test_profile_header_refresh_ignores_unreadable_ref_or_invalid_utf8(self):
        profile = self.profile()
        self.profile_git(profile, "update-ref", "-d", "refs/remotes/origin/main")
        self.assertEqual(self.actions("records"), [])
        self.assertEqual(self.actions("repair"), [])
        profile.write_bytes(b"\xff")
        self.save_profile(profile)
        self.assertEqual(self.actions("records"), [])
        self.assertEqual(self.actions("repair"), [])
        with patch.object(nextaction, "run", side_effect=PermissionError("git objects are not readable")):
            self.assertIsNone(nextaction.profile_header_action(profile))

    def test_profile_header_partial_clone_read_does_not_fetch_or_write_objects(self):
        self.header_projection.stop()
        profile = pathlib.Path(CONFIG["repos_dir"]) / (CONFIG["github_user"] + "__" + CONFIG["github_user"]) / "README.md"
        seed = self.root / "seed/README.md"
        seed.parent.mkdir()
        self.profile_git(seed, "init", "-b", "main")
        self.profile_git(seed, "config", "uploadpack.allowFilter", "true")
        for content in ("first", "second"):
            seed.write_text(profile_readme.PORTFOLIO_START + "\n" + content + "\n" + profile_readme.PORTFOLIO_END)
            self.save_profile(seed, published=False)
            if content == "first":
                run(["git", "clone", "--filter=blob:none", seed.parent.as_uri(), str(profile.parent)])
            else:
                self.profile_git(profile, "fetch", "origin")
        blob = self.profile_git(seed, "rev-parse", "HEAD:README.md")
        self.assertIn("?" + blob, self.profile_git(profile, "rev-list", "--objects", "--missing=print", "origin/main"))
        before = {p: p.read_bytes() for p in profile.parent.rglob("*") if p.is_file()}
        trace = self.root / "profile-read.trace"
        with patch.dict(os.environ, {"GIT_TRACE": str(trace)}):
            self.assertEqual(self.actions("records"), [])
        self.assertNotIn("fetch", trace.read_text())
        self.assertEqual(before, {p: p.read_bytes() for p in profile.parent.rglob("*") if p.is_file()})
        self.assertIn("?" + blob, self.profile_git(profile, "rev-list", "--objects", "--missing=print", "origin/main"))

    def test_profile_header_read_has_a_timeout_and_ignores_expiration(self):
        profile = self.profile()
        with patch("factory.util.subprocess.run", side_effect=subprocess.TimeoutExpired("git show", 5)) as process:
            self.assertIsNone(nextaction.profile_header_action(profile))
        self.assertEqual(process.call_args.kwargs["timeout"], 5)
        self.assertEqual(process.call_args.kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")

    def test_profile_header_id_tracks_expected_content_not_clock_or_manual_edits(self):
        profile = self.profile()
        first, = self.actions("records")
        self.assertEqual(self.actions("records")[0]["id"], first["id"])
        profile.write_text("# Different manual text\n" + profile.read_text().replace("stale header", "other stale header"))
        self.clock += dt.timedelta(hours=1)
        self.assertEqual(self.actions("records")[0]["id"], first["id"])
        self.clock += dt.timedelta(hours=23)
        self.assertEqual(nextaction.profile_header_action(profile)["id"], first["id"])
        write_json(self.ledger.with_name("pr-history.json"), {
            "schema_version": 1, "prs": {}, "reconciliation": {"checked_at": self.clock.isoformat()}})
        second, = self.actions("records")
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(self.actions("records")[0]["id"], second["id"])

    def test_profile_header_follows_merge_records_ci_review_and_ship(self):
        self.profile()
        receipt = {"at": self.clock.isoformat(), "ref": "commit", "sha256": "1" * 64}
        self.row(status="merged", records={"profile": receipt})
        self.review(self.item("review", "needs_coordinator_review"))
        self.item("ready", "ship_ready")
        self.event("ci-failing", {"url": "https://github.com/o/r/pull/2", "repo": "o/r", "number": 2},
                   {"jobs": [{"name": "test"}]})
        entries = self.actions()
        self.assertEqual([entry["kind"] for entry in entries], [
            "ci-triage", "coordinator-review", "ship", "records", "records", "records", "report"])
        self.assertEqual([entry["target"] for entry in entries if entry["kind"] == "records"], ["teach", "notebook", "profile-header"])
        write_json(self.ledger, [{**read_json(self.ledger)[0], "records": {"profile": {"unverified": True}}}])
        self.assertEqual([entry["target"] for entry in self.actions("records")], ["teach", "notebook", "audit", "profile-header"])

    def test_profile_header_refresh_yields_to_feedback_and_review_waves(self):
        self.profile()
        review = self.item("review", "stale_reviews", repo="o/review")
        self.review(review)
        follow = self.item("follow", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = self.row(item=str(follow.dir.relative_to(self.root)))
        self.event("maintainer-comment", row)
        for roles in ({"follow", "review"}, {"follow"}):
            with self.subTest(roles=roles):
                entries = self.actions()
                wave = next(entry for entry in entries if entry["kind"] == "wave")
                header = next(entry for entry in entries if entry["target"] == "profile-header")
                self.assertEqual({task["role"] for task in nextaction.wave_tasks(wave)}, roles)
                self.assertLess(entries.index(wave), entries.index(header))
            self.plan["stale_reviews"] = []
        self.plan["stale_reviews"] = [{"item": str(review.dir.relative_to(self.root)), "reasons": []}]
        self.ledger.with_name("followup-events.jsonl").unlink()
        self.assertEqual([entry["kind"] for entry in self.actions()], ["wave", "records", "report"])

    def test_history_refresh_suppresses_header_until_an_attempt_is_saved(self):
        self.profile()
        path = self.ledger.with_name("pr-history.json")
        for cache in (None, {"schema_version": 1, "prs": {}}, {"schema_version": 1, "prs": {}, "reconciliation": {"checked_at": "invalid"}},
                      {"schema_version": 1, "prs": {}, "reconciliation": {"checked_at": (self.clock - dt.timedelta(hours=13)).isoformat()}}):
            with self.subTest(cache=cache):
                if cache is None:
                    path.unlink()
                else:
                    write_json(path, cache)
                with patch.object(profile_readme, "portfolio", side_effect=AssertionError("history must be attempted first")):
                    self.assertEqual([entry["target"] for entry in self.actions("records")], ["history"])
        write_json(path, {"schema_version": 1, "prs": {}, "reconciliation": {"checked_at": self.clock.isoformat(), "error": "transport_error"}})
        self.assertEqual([entry["target"] for entry in self.actions("records")], ["profile-header"])

    def test_history_then_header_close_after_waves_release_commit_and_measure(self):
        self.clock = dt.datetime(2026, 11, 8, 16, tzinfo=dt.timezone.utc)
        self.profile()
        self.lanes = ["o/new"]
        self.status = " M ledger/prs.json\0"
        self.ledger.with_name("pr-history.json").unlink()
        release = nextaction.action("release", "idle", [], "Release", "", [], "")
        with patch.object(nextaction, "release_action", return_value=release):
            entries = self.actions()
            self.assertEqual([entry["kind"] for entry in entries], ["wave", "release", "commit", "measure", "records", "report"])
            self.assertEqual(entries[-2]["target"], "history")
            write_json(self.ledger.with_name("pr-history.json"), {"schema_version": 1, "prs": {}, "reconciliation": {"checked_at": self.clock.isoformat()}})
            entries = self.actions()
            self.assertEqual([entry["kind"] for entry in entries], ["wave", "release", "commit", "measure", "records", "report"])
            self.assertEqual(entries[-2]["target"], "profile-header")
            late = [nextaction.action("records", target, [], target, "", [], "") for target in ("profile-header", "history")]
            with patch.object(nextaction, "record_actions", return_value=late):
                entries = self.actions()
            self.assertEqual([entry["kind"] for entry in entries], ["wave", "release", "commit", "measure", "records", "records", "report"])
            self.assertEqual([entry["target"] for entry in entries[-3:-1]], ["history", "profile-header"])

    def test_profile_merge_record_waits_for_the_history_refresh_and_publishes_once(self):
        self.profile()
        self.row(status="merged", merged_at=self.clock.isoformat())
        self.ledger.with_name("pr-history.json").unlink()
        entries = self.actions()
        records = [entry["target"] for entry in entries if entry["kind"] == "records"]
        self.assertEqual(records, ["teach", "notebook", "history", "profile"])
        self.assertEqual(entries[-1]["kind"], "report")
        write_json(self.ledger.with_name("pr-history.json"), {"schema_version": 1, "prs": {}, "reconciliation": {"checked_at": self.clock.isoformat()}})
        records = [entry["target"] for entry in self.actions() if entry["kind"] == "records"]
        self.assertEqual(records, ["profile", "teach", "notebook"])

    def test_broken_profile_projection_does_not_hide_other_record_work(self):
        self.profile()
        receipt = {"at": self.clock.isoformat(), "ref": "commit", "sha256": "1" * 64}
        self.row(status="merged", records={"profile": receipt})
        with patch.object(profile_readme, "portfolio", side_effect=ValueError("invalid highlights")):
            self.assertEqual([entry["target"] for entry in self.actions("records")], ["teach", "notebook"])

    def test_invalid_profile_destinations_do_not_hide_pending_ci_or_merge_records(self):
        self.profile()
        receipt = {"at": self.clock.isoformat(), "ref": "commit", "sha256": "1" * 64}
        self.row(status="merged", records={"profile": receipt})
        self.event("ci-failing", {"url": "https://github.com/o/r/pull/2", "repo": "o/r", "number": 2},
                   {"jobs": [{"name": "test"}]})
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        for public in (None, {}, {"repository": "example/factory", "dashboard_url": "https://example.invalid/"}):
            with self.subTest(public=public), patch.dict(CONFIG, {"public": public}):
                if public is None:
                    del CONFIG["public"]
                error = io.StringIO()
                with redirect_stderr(error):
                    code, output = self.run_cli("next", "--all", "--json")
                self.assertEqual(code, 0, error.getvalue())
                self.assertEqual(error.getvalue(), "")
                entries = json.loads(output)
                self.assertEqual([entry["kind"] for entry in entries], ["ci-triage", "records", "records", "report"])
                self.assertEqual([entry["target"] for entry in entries if entry["kind"] == "records"], ["teach", "notebook"])
                self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertEqual([entry["target"] for entry in self.actions("records")], ["teach", "notebook", "profile-header"])

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
        kinds = [action["kind"] for action in self.actions()]
        self.assertEqual(kinds[0], "ship", "a silent PR's missing lesson does not hold other new PRs")
        silent, = [action for action in self.actions() if action["kind"] == "silence-lesson"]
        self.assertIn("does not block other new PRs", silent["why"])
        self.assertLess(kinds.index("ship"), kinds.index("silence-lesson"))

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
        self.plan["repo_slots"] = [{"repo": repo, "merged": 1} for repo in ("o/r", "o/scout")]
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
        self.plan["repo_slots"] = [{"repo": "o/scout", "merged": 1}]
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
