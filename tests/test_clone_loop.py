import contextlib
import io
import shutil
import sys
import unittest
from unittest.mock import patch

import test_clones
from support import ROOT, setup_module
from factory import cli
from factory.core import briefs, clean, clones, gate, nextaction, receipts, redgreen, review, slop, stopcheck, verdicts
from factory.oss import plan, ship, submit
from factory.util import CONFIG, CommandError, Item, git, write_json


def setUpModule():
    setup_module(unittest)


class ReleasedLoop(unittest.TestCase):
    use = test_clones.Clones.use
    command = test_clones.Clones.command
    identity = test_clones.Clones.identity
    make_item = test_clones.Clones.make_item
    commit = test_clones.Clones.commit
    record = test_clones.Clones.record

    def setUp(self):
        test_clones.Clones.setUp(self)
        self.use(patch.dict(CONFIG["limits"], max_soft_slop_hits=3))
        self.use(patch.object(briefs, "ROOT", ROOT))
        self.head = self.commit(push=True)
        self.item.meta.update(schema=1, line="oss", status="ready", implementer="builder")
        self.item.save()
        self.item.file("verify.md").write_text("verdict: ship\nWho hits it: parser users\nImpact: retain titles\n")
        self.item.file("pr.md").write_text("# fix: retain titles\n\nKeep parsed titles.\n")
        self.item.file("review.md").write_text(f"head: {self.head}\nreviewer: independent\nverdict: ship\n")
        self.item.file("coordinator-review.md").write_text(f"head: {self.head}\nverdict: ship\n")
        for module in (nextaction, receipts, stopcheck, ship):
            self.use(patch.object(module, "ROOT", self.root))
        self.use(patch.object(nextaction.lanes, "order", return_value=[]))
        self.use(patch.object(nextaction.lessons, "lint", return_value=[]))
        self.use(patch.object(nextaction, "commit_action", return_value=None))
        self.use(patch.object(nextaction, "record_actions", return_value=[]))
        self.use(patch.object(nextaction, "observations", return_value=([], "saved")))

    def release(self):
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        self.assertFalse(self.checkout.exists())

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = cli.main(list(map(str, args)))
        return result, output.getvalue()

    def restored(self, item=None):
        item = item or self.item
        self.assertTrue(self.checkout.is_dir())
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), self.head)
        self.assertEqual(item.head_sha(), self.head)
        self.assertIsNone(clones.released(item))

    def test_classification_keeps_review_buckets_without_git(self):
        cases = [("ship", True, "ship_ready"), ("ship", False, "needs_coordinator_review"),
                 ("fix-needed", False, "fix_round"), (None, False, "unreviewed")]
        for verdict, coordinator, expected in cases:
            with self.subTest(bucket=expected):
                if verdict:
                    self.item.file("review.md").write_text(f"head: {self.head}\nreviewer: independent\nverdict: {verdict}\n")
                else:
                    self.item.file("review.md").unlink(missing_ok=True)
                if not coordinator:
                    self.item.file("coordinator-review.md").unlink(missing_ok=True)
                self.assertEqual(plan.classify(self.item)[0], expected)
                self.release()
                with patch.object(verdicts, "git", side_effect=AssertionError("released classification must not run git")):
                    self.assertEqual(plan.classify(self.item)[0], expected)
                clones.ensure(self.item)

    def test_released_stale_heads_compare_commit_ids_only(self):
        self.item.file("review.md").write_text(f"head: {self.base}\nreviewer: independent\nverdict: ship\n")
        self.assertEqual(plan.classify(self.item)[0], "stale_reviews")
        self.release()
        with patch.object(verdicts, "git", side_effect=AssertionError("no checkout")):
            self.assertEqual(plan.classify(self.item)[0], "stale_reviews")
            self.assertFalse(verdicts.bound(self.item, self.head, self.base))
            self.assertTrue(verdicts.bound(self.item, self.head, self.head[:12]))

    def test_shared_clone_keeps_other_released_item_on_recorded_bindings(self):
        other = self.make_item("second")
        other_head = self.commit(other, push=True)
        self.release()
        clones.ensure(self.item)
        self.assertIsNotNone(clones.released(other))
        with patch.object(verdicts, "git", side_effect=AssertionError("other branch is still released")):
            self.assertFalse(verdicts.bound(other, other_head, self.base))
        with patch.object(stopcheck, "ready_items", return_value=[other]), \
                patch.object(gate, "core_checks", side_effect=AssertionError("other branch is still released")), \
                patch.object(stopcheck.ledger, "missing_lessons", return_value=[]):
            self.assertIn("clone released", stopcheck.problems()[0])

    def build_candidate(self):
        self.item.meta["schema"] = 2
        self.item.save()
        write_json(self.item.file("demand.json"), {
            "claim": "Retain parsed titles", "who_hits_it": {"kind": "external-report", "source": "https://example.org/report",
            "quote": "The title is lost.", "self_filed": False}, "contract": {}, "workaround": "Reenter title",
            "error_path": False, "real_occurrence": "", "decision": "build"})
        write_json(self.item.file("value-review.json"), {
            "reviewer": "value-reviewer", "verdict": "build", "demand_sha256": verdicts.demand_sha256(self.item),
            "answers": {key: {"ok": True, "note": "Read the report"} for key in review.VALUE_ANSWERS}})

    def test_schema2_released_build_keeps_implementation_stage(self):
        self.build_candidate()
        self.assertEqual(plan.classify(self.item)[0], "unreviewed")
        self.release()
        self.assertEqual(plan.classify(self.item)[0], "unreviewed")

    def test_committed_template_remains_active_and_keeps_its_checkout(self):
        self.build_candidate()
        self.item.meta["status"] = "preparing"
        self.item.save()
        self.item.file("verify.md").write_text((ROOT / "lines/oss/templates/verify.md").read_text())
        write_json(self.item.file(review.timeline.FILE_NAME), [{"event": "role-started", "role": "implement"}])
        self.assertNotEqual(self.item.head_sha(), self.item.merge_base())
        pending = nextaction.actions(observe=False, include_release=False)
        tasks = [task for entry in pending for task in nextaction.wave_tasks(entry)]
        with self.subTest(check="task"):
            self.assertEqual([(task["role"], task["item"]) for task in tasks], [("implement", nextaction.item_target(self.item))])
        active = nextaction.code_items(pending)
        with self.subTest(check="active"):
            self.assertEqual(active, {self.item.dir})
        result = clones.release_idle(active)
        with self.subTest(check="release"):
            self.assertEqual(result["reports"], [])
        self.assertTrue(self.checkout.is_dir())
        self.assertEqual(self.item.head_sha(), self.head)

    def test_missing_clone_without_record_stays_repair(self):
        shutil.rmtree(self.checkout)
        self.assertEqual(plan.classify(self.item)[0], "missing_clone")

    def test_code_briefs_restore_before_composing(self):
        for role in ("implement", "review", "fix", "follow", "coordinator-review", "takeover"):
            with self.subTest(role=role):
                self.release()
                code, text = self.invoke("brief", role, "--item", self.item.dir)
                self.assertIn(code, (None, 0))
                self.restored()
                self.assertIn(self.head, text)

    def test_scout_prepares_local_repository_before_composing(self):
        self.release()
        original = clones._clone
        def local_clone(checkout, origin, remotes):
            original(checkout, self.upstream.as_uri(), {})
        with patch.object(clones, "_clone", side_effect=local_clone):
            code, text = self.invoke("brief", "scout", "--repo", "example/project")
        self.assertIn(code, (None, 0))
        self.assertTrue(self.checkout.is_dir())
        self.assertIn(str(self.checkout), text)
        self.assertIsNotNone(self.record())

    def test_gate_restores_before_checks(self):
        self.release()
        def run_gate(item, **options):
            self.restored(item)
            return gate.Report(), {"head_sha": self.head, "ok": True}
        with patch.object(gate, "run", side_effect=run_gate):
            self.assertEqual(self.invoke("gate", self.item.dir, "--mode", "prepare")[0], 0)

    def test_redgreen_slop_and_submit_restore_before_work(self):
        for name, module, function in (("redgreen", redgreen, "run"), ("slop", slop, "check"), ("submit", submit, "run")):
            with self.subTest(command=name):
                self.release()
                def operation(item, **options):
                    self.restored(item)
                    return {"ok": False}
                with patch.object(module, function, side_effect=operation):
                    self.assertEqual(self.invoke(name, self.item.dir)[0], 1)

    def test_ship_restores_before_review_or_publication(self):
        self.release()
        def disclosure(flow):
            self.restored(flow.item)
            raise ship.Stop("stop before publication")
        with patch.object(ship, "linked_worktree", return_value=False), patch.object(ship.Ship, "disclosure", disclosure):
            code, output = self.invoke("ship", self.item.dir, "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("stop before publication", output)

    def test_open_issue_restores_before_built_branch_check(self):
        self.item.file("issue.md").write_text("# Titles are lost\n\nParse a named title.\n")
        self.invoke("approve-proposal", self.item.dir, "--file", "issue.md", "--note", "Approved draft")
        self.release()
        def count(*args, **options):
            self.restored()
            return "0"
        with patch.object(cli, "publication_findings", return_value=([], [])), patch.object(cli, "git", side_effect=count):
            with self.assertRaisesRegex(SystemExit, "fix is not built"):
                self.invoke("open-issue", self.item.dir)

    def test_ensure_failure_stops_commands_with_its_message(self):
        self.release()
        commands = [("brief", "review", "--item", self.item.dir), ("gate", self.item.dir),
                    ("redgreen", self.item.dir), ("slop", self.item.dir), ("submit", self.item.dir),
                    ("run", "--item", self.item.dir, "--", sys.executable, "-c", "pass")]
        with patch.object(clones, "ensure", side_effect=CommandError("restore the saved source before retrying")):
            for args in commands:
                with self.subTest(command=args[0]):
                    code, text = self.invoke(*args)
                    self.assertNotEqual(code, 0)
                    self.assertIn("restore the saved source before retrying", text)
        self.assertFalse(self.checkout.exists())

    def test_run_and_rerun_restore_clone_and_subdirectories(self):
        git(self.checkout, "checkout", self.item.meta["branch"])
        (self.checkout / "subdir").mkdir()
        (self.checkout / "subdir" / "tracked.txt").write_text("proof\n")
        git(self.checkout, "add", ".")
        git(self.checkout, "commit", "-m", "add proof directory")
        git(self.checkout, "push", "fork", self.item.meta["branch"])
        self.head = self.item.head_sha()
        self.release()
        first = receipts.run_cmd(self.item, [sys.executable, "-c", "print('proof')"], cwd="subdir")
        self.restored()
        self.release()
        second = receipts.rerun(self.item, first["id"])
        self.restored()
        self.assertTrue(second["match"]["exit"])
        self.assertTrue(second["match"]["output"])

    def test_external_receipt_cwd_does_not_restore_clone(self):
        self.release()
        with patch.object(clones, "ensure", side_effect=AssertionError("external cwd needs no clone")):
            proof = receipts.run_cmd(self.item, [sys.executable, "-c", "print('external')"], cwd=self.seed)
            repeated = receipts.rerun(self.item, proof["id"])
        self.assertEqual(repeated["exit"], 0)
        self.assertFalse(self.checkout.exists())

    def test_review_stamp_uses_recorded_head_without_cloning(self):
        self.release()
        with patch.object(clones, "ensure", side_effect=AssertionError("stamp only needs the recorded head")):
            self.invoke("review", "stamp", "--item", self.item.dir, "review", "--head", self.head)
            self.assertIn(self.head, self.item.file("review.md").read_text())
            item = Item(self.item.dir)
            item.meta["schema"] = 2
            item.save()
            write_json(item.file("review.json"), {"verdict": "ship"})
            self.invoke("review", "stamp", "--item", item.dir, "review", "--head", self.head)
            self.assertEqual(verdicts.read_review(item)["head"], self.head)
        self.assertFalse(self.checkout.exists())

    def test_release_action_lists_refusals_and_disappears(self):
        self.use(patch.object(clones, "now", return_value=clones.now()))
        self.item.meta["status"] = "parked"
        self.item.save()
        dirty = self.checkout.with_name("dirty")
        self.command("git", "clone", self.upstream.as_uri(), str(dirty))
        (dirty / "untracked.txt").write_text("keep me\n")
        entries = nextaction.actions(observe=False)
        action = next(entry for entry in entries if entry["kind"] == "release")
        self.assertEqual(action["title"], "Release idle clones")
        self.assertEqual(action["commands"][0], "bin/factory clone release --idle")
        self.assertIn("commit action", action["commands"][1])
        self.assertIn(str(self.checkout), action["why"])
        self.assertIn(str(dirty), action["why"])
        self.assertIn("untracked", action["why"])
        self.assertIn(str(clean.size(self.checkout)), action["why"])
        self.assertLess(nextaction.ORDER["release"], nextaction.ORDER["commit"])
        self.assertLess(nextaction.ORDER["release"], nextaction.ORDER["measure"])
        with self.assertRaisesRegex(ValueError, "not ack"):
            nextaction.acknowledge(action["id"], "released")
        report = clones.release_idle(nextaction.code_items())
        self.assertTrue(any(row["released"] for row in report["reports"]))
        self.assertEqual([entry for entry in nextaction.actions(observe=False) if entry["kind"] == "release"], [])
        self.assertTrue((dirty / "untracked.txt").exists())

    def test_review_work_protects_checkout_from_idle_release(self):
        self.item.file("review.md").unlink()
        active = nextaction.code_items()
        self.assertIn(self.item.dir, active)
        self.assertEqual(clones.release_idle(active)["reports"], [])
        self.assertTrue(self.checkout.is_dir())

    def test_released_coordinator_actions_print_self_restoring_commands(self):
        for kind in ("coordinator-review", "ship", "takeover"):
            with self.subTest(kind=kind):
                self.item.file("review.md").write_text(f"head: {self.head}\nreviewer: independent\nverdict: ship\n")
                self.item.file("coordinator-review.md").write_text(f"head: {self.head}\nverdict: ship\n")
                if kind == "coordinator-review":
                    self.item.file("coordinator-review.md").unlink()
                if kind == "takeover":
                    text = f"head: {self.head}\nreviewer: independent\nverdict: fix-needed\n"
                    self.item.file("review.md").write_text(text)
                    self.item.file("review-round1.md").write_text(text)
                self.release()
                action = next(row for row in nextaction.actions(observe=False) if row["kind"] == kind)
                target = nextaction.item_target(self.item)
                if kind == "ship":
                    expected = [nextaction.command("ship", target, "--dry-run"), nextaction.command("ship", target)]
                    self.assertEqual(action["commands"], expected)
                else:
                    self.assertEqual(action["commands"][0], nextaction.command("brief", kind, "--item", target))
                self.assertNotIn("clone ensure", " ".join(action["commands"]))
                clones.ensure(self.item)

    def test_disk_action_starts_with_idle_release(self):
        with patch.object(nextaction.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 99, 1)):
            action = next(entry for entry in nextaction.actions(observe=False) if entry["kind"] == "disk")
        self.assertEqual(action["commands"][0], "bin/factory clone release --idle")

    def test_stopcheck_reports_released_ready_item_without_cloning(self):
        self.release()
        with patch.object(clones, "ensure", side_effect=AssertionError("Stop must not clone")), \
                patch.object(gate, "core_checks", side_effect=AssertionError("released item has no checkout")), \
                patch.object(stopcheck.ledger, "missing_lessons", return_value=[]):
            problems = stopcheck.problems()
        self.assertEqual(problems, [f"{self.item.dir.name}: clone released; run bin/factory clone ensure --item {self.item.dir} before finishing it"])
        self.assertFalse(self.checkout.exists())

    def test_clean_help_names_idle_release(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            cli.main(["clean", "--help"])
        self.assertIn("bin/factory clone release --idle", " ".join(output.getvalue().split()))
