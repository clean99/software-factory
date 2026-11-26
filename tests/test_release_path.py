"""Release modes, coordinator checks and bounded release previews."""
import contextlib
import copy
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import is_pulls_read, isolated_config, rest_pulls, setup_module

from factory import cli
from factory.core import checks, gate, ledger, lessons, review as protocol, shiftlock, verdicts

RUN_EXTRA = checks.run_extra
from factory.oss import disclosure, plan, policy, qualify, ship, submit
from factory.oss.submit import preconditions as submit_preconditions
from factory.util import CONFIG, Item, iso, parse_iso, read_json, write_json

HEAD = "a" * 40
OLD = "b" * 40
URL = "https://github.com/example/repo/pull/7"
SHIFT_STARTED = "2026-11-22T02:00:00+00:00"
VERIFY = "verdict: ship\nWho hits it: reporters using the documented parser\nImpact: parsed titles are retained in the result\n"


def setUpModule():
    setup_module(unittest)


class ReleaseCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name).resolve()
        self.runs = self.root / "runs"
        self.folder = self.runs / "2026-11-10" / "parser"
        self.folder.mkdir(parents=True)
        self.clone = self.root / "clone"
        self.clone.mkdir()
        self.ledger = self.root / "prs.json"
        self.lessons = self.root / "lessons.txt"
        write_json(self.ledger, [])
        self.lessons.write_text("")
        self.extra = Mock()
        for mocker in (
            patch.dict(CONFIG, runs_dir=str(self.runs), ledger_file=str(self.ledger),
                       lessons_file=str(self.lessons), repos_dir=str(self.root / "repos"),
                       standing_approval_note="Standing release permission."),
            patch.object(Item, "head_sha", return_value=HEAD),
            patch.object(Item, "merge_base", return_value=OLD),
            patch.object(cli.clones, "ensure"),
            patch.object(gate, "git", return_value=""),
            patch.object(gate, "sh", return_value=Mock(returncode=0)),
            patch.object(gate.slop, "check", return_value={"ok": True, "stats": {"files": 2}, "fails": [], "warns": []}),
            patch("factory.util.global_hooks_drift", return_value=[]),
            patch.object(verdicts, "recommitted", return_value=False),
            patch.object(checks, "run_extra", self.extra),
            # The new-PR count reads this checkout's lease; keep it local to the test.
            patch.object(shiftlock, "ROOT", self.root),
        ):
            mocker.start()
            self.addCleanup(mocker.stop)
        write_json(self.folder / "item.json", {
            "line": "oss", "repo": "example/repo", "branch": "contrib/parser", "base": "main",
            "clone": str(self.clone), "status": "preparing", "kind": "small", "implementer": "builder",
            "issue": 2, "pushed_head": OLD,
        })
        self.item = Item(self.folder)
        for name, text in {
            "pr.md": "# fix: retain parsed titles\n\nFixes #2\n",
            "validation.md": "The parser test passes.\n", "interview.md": "Parser behavior.\n",
            "verify.md": VERIFY, "review.md": f"reviewer: reviewer\nhead: {HEAD}\nverdict: ship\n",
            "coordinator-review.md": f"head: {HEAD}\nverdict: ship\n",
        }.items():
            self.item.file(name).write_text(text)
        write_json(self.item.file("redgreen.json"), {"ok": True, "head_sha": HEAD, "red_exit": 1, "green_exit": 0})
        write_json(self.item.file("qualify.json"), {"at": iso(), "verdict": "ok"})
        write_json(self.item.file("policy.json"), {"version": policy.VERSION, "at": iso(), "flags": {}})
        write_json(self.item.file("approval.json"), {"at": iso(), "head_sha": HEAD, "pr_sha256": verdicts.pr_sha256(self.item)})

    def schema2(self):
        self.item.meta.update(schema=2, required_checks=[], tags=[])
        self.item.save()
        for name in ("review.md", "validation.md", "coordinator-review.md"):
            self.item.file(name).unlink(missing_ok=True)
        self.item.file("verify.md").write_text("verdict: ship\n")
        self.item.meta.update(test_cmd="python -m unittest test_parser", required_checks=[
            {"label": "test", "argv": ["python", "-m", "unittest", "test_parser"], "cwd": "."}])
        self.item.save()
        write_json(self.item.file("redgreen.json"), {"ok": True, "head_sha": HEAD, "red_exit": 1, "green_exit": 0,
                                                   "cmd": self.item.meta["test_cmd"], "cwd": "."})
        review = {"reviewer": "reviewer", "head": HEAD, "verdict": "ship", "pr_sha256": verdicts.pr_text_sha256(self.item),
                  "obligations": [{"class": row["id"], "applies": False, "why": "The parser does not reach this path.", "receipts": []}
                                  for row in protocol.classes()], "findings": []}
        write_json(self.item.file("review.json"), review)
        write_json(self.item.file("coordinator-review.json"), {
            "head": HEAD, "verdict": "ship", "pr_sha256": verdicts.pr_text_sha256(self.item),
            "read": dict.fromkeys(("diff", "tests", "pr_text", "demand"), True), "reruns": ["rerun"],
            "counterexample": {"what": "Try a truncated token.", "receipt": "counter"}})
        write_json(self.item.file("demand.json"), {
            "claim": "Parsed titles are retained", "who_hits_it": {"kind": "external-report", "source": "https://example.org/report",
            "quote": "The parser loses a title.", "self_filed": False}, "contract": {}, "workaround": "Reenter the title.",
            "error_path": False, "real_occurrence": "", "decision": "build"})
        write_json(self.item.file("value-review.json"), {
            "reviewer": "value-reviewer", "verdict": "build", "demand_sha256": verdicts.demand_sha256(self.item),
            "answers": {key: {"ok": True, "note": "Checked the external report."} for key in protocol.VALUE_ANSWERS}})
        receipts = {name: {"id": name, "cwd": str(self.clone), "cwd_head": HEAD, "item_head": HEAD, "exit": 0,
                           "clean": True, "head_before": HEAD, "head_after": HEAD,
                           "tracked_dirty_before": False, "tracked_dirty_after": False,
                           "argv": ["python", "probe.py"] if name == "counter" else ["python", "-m", "unittest", "test_parser"]}
                    for name in ("test", "rerun", "counter")}
        receipts["rerun"].update(rerun_of="test", match={"exit": True, "output": True})
        for mocker in (patch.object(protocol.receipts, "load", return_value=list(receipts.values())),
                       patch.object(protocol.receipts, "get", side_effect=lambda item, rid: receipts.get(rid)),
                       patch.object(protocol.receipts, "valid", return_value=True),
                       patch.object(checks, "run", return_value=Mock(returncode=1, stdout=""))):
            mocker.start()
            self.addCleanup(mocker.stop)

    def snapshot(self):
        return {str(path.relative_to(self.folder)) + ("/" if path.is_dir() else ""):
                b"" if path.is_dir() else path.read_bytes() for path in self.folder.rglob("*")}

    def assert_cache_changes_only(self, before):
        after = self.snapshot()
        changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
        self.assertLessEqual(changed, {"gate.json", "slop.json", "policy.json", "qualify.json"})

    def row(self, report, name):
        return next(r for r in report.rows if r["check"] == name)

    def state(self):
        return self.item.meta_path.read_bytes(), self.ledger.read_bytes(), copy.deepcopy(self.item.meta)

    def cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return cli.main(list(args))


class GateModes(ReleaseCase):
    def test_modes_omit_only_later_stage_rows_for_both_schemas(self):
        for schema in (1, 2):
            if schema == 2:
                self.schema2()
            for mode in ("prepare", "review", "release"):
                with self.subTest(schema=schema, mode=mode):
                    rep, result = gate.run(self.item, mode=mode)
                    self.assertTrue(rep.ok, rep.text())
                    names = {r["check"] for r in rep.rows}
                    self.assertEqual("independent review" in names, mode != "prepare")
                    self.assertEqual("review is of this HEAD" in names, mode != "prepare")
                    self.assertEqual("coordinator review" in names, mode == "release")
                    self.assertEqual("human approval" in names, mode == "release")
                    self.assertEqual("user impact stated" in names, schema == 1)
                    self.assertEqual(result["mode"], mode)
                    self.extra.assert_called_with(self.item, rep, mode)
            self.assertEqual(gate.run(self.item)[1]["mode"], "review")
            self.assertEqual(gate.run(self.item, mode="prepare", with_approval=True)[1]["mode"], "release")
            self.assertEqual(self.row(rep, "human approval")["fix"],
                             "complete the coordinator review of this head and PR text, then run bin/factory ship <item>, "
                             "which records the owner's standing approval; bin/factory approve <item> --note '<owner quote>' is for a manual submit")

    def test_prepare_does_not_require_the_independent_review_artifact(self):
        for schema in (1, 2):
            if schema == 2:
                self.schema2()
            self.item.file("review.json" if schema == 2 else "review.md").unlink()
            rep, _ = gate.run(self.item, mode="prepare")
            self.assertTrue(rep.ok, rep.text())
            rep, _ = gate.run(self.item, mode="review")
            self.assertFalse(self.row(rep, "independent review")["ok"])

    def test_schema2_reads_json_even_when_markdown_says_ship(self):
        self.schema2()
        self.item.file("review.md").write_text(f"reviewer: reviewer\nhead: {HEAD}\nverdict: ship\n")
        write_json(self.item.file("review.json"), {"reviewer": "reviewer", "head": OLD, "verdict": "fix-needed"})
        rep, _ = gate.run(self.item)
        self.assertFalse(self.row(rep, "independent review")["ok"])
        self.assertFalse(self.row(rep, "review is of this HEAD")["ok"])
        self.assertTrue(self.row(rep, "artifacts")["ok"])
        self.item.file("review.json").unlink()
        self.assertFalse(self.row(gate.run(self.item)[0], "independent review")["ok"])

    def test_large_schema2_still_requires_design_and_independent_reviewer(self):
        self.schema2()
        self.item.meta.update(kind="large", implementer="reviewer")
        rep, _ = gate.run(self.item)
        self.assertFalse(self.row(rep, "artifacts")["ok"])
        self.assertIn("design.md", self.row(rep, "artifacts")["detail"])
        self.assertFalse(self.row(rep, "independent review")["ok"])

    def test_coordinator_requires_current_head_ship_and_schema2_pr_hash(self):
        self.schema2()
        current = {"head": HEAD, "verdict": "ship", "pr_sha256": verdicts.pr_sha256(self.item)}
        cases = ({}, {"pr_sha256": None}, {"pr_sha256": "wrong"}, {"head": OLD}, {"verdict": "drop"})
        for changes in cases:
            with self.subTest(changes=changes):
                write_json(self.item.file("coordinator-review.json"), {**current, **changes})
                rep, _ = gate.run(self.item, mode="release")
                self.assertEqual(self.row(rep, "coordinator review")["ok"], not changes)
        write_json(self.item.file("coordinator-review.json"), current)
        self.item.file("pr.md").write_text("# fix: changed description\n")
        rep, _ = gate.run(self.item, mode="release")
        self.assertFalse(self.row(rep, "coordinator review")["ok"])
        self.assertIn("pr_sha256", self.row(rep, "coordinator review")["detail"])

    def test_extra_checks_run_after_oss_checks_and_can_refuse(self):
        def extra(item, rep, mode):
            self.assertIn("issue still free", [r["check"] for r in rep.rows])
            rep.add("package check", False, "missing evidence")

        self.extra.side_effect = extra
        rep, result = gate.run(self.item)
        self.assertFalse(rep.ok)
        self.assertFalse(result["ok"])
        with self.assertRaisesRegex(ValueError, "prepare, review or release"):
            gate.run(self.item, mode="invalid")

    def test_only_successful_review_mode_advances_preparing_to_ready(self):
        for args, expected in (([], "ready"), (["--mode", "review"], "ready"),
                               (["--mode", "prepare"], "preparing"), (["--mode", "release"], "preparing"),
                               (["--approval"], "preparing"), (["--mode", "review", "--approval"], "preparing")):
            with self.subTest(args=args):
                self.item.meta["status"] = "preparing"
                self.item.save()
                self.assertEqual(self.cli("gate", str(self.folder), *args), 0)
                self.assertEqual(Item(self.folder).meta["status"], expected)
        self.item.meta["status"] = "approved"
        self.item.save()
        self.assertEqual(self.cli("gate", str(self.folder)), 0)
        self.assertEqual(Item(self.folder).meta["status"], "approved")
        self.item.meta["status"] = "preparing"
        self.item.save()
        self.item.file("review.md").unlink()
        self.assertEqual(self.cli("gate", str(self.folder)), 1)
        self.assertEqual(Item(self.folder).meta["status"], "preparing")


class SubmitPreview(ReleaseCase):
    def setUp(self):
        super().setUp()
        self.prs = [{"url": URL, "state": "OPEN", "createdAt": "2026-11-01T00:00:00Z"}]
        self.remote = OLD
        self.remotes = "origin\nfork"
        self.commands = []

        def git(clone, *args, **kwargs):
            self.commands.append(args)
            if args == ("remote",):
                return self.remotes
            if args[0] == "ls-remote":
                return f"{self.remote}\trefs/heads/contrib/parser" if self.remote else ""
            if args[0] == "push":
                self.remote = args[-1].split(":", 1)[0]
            return ""

        self.process = Mock(return_value=Mock(returncode=0, stdout=URL))
        self.target = Mock(return_value=[])
        self.push_scan = Mock(return_value=[])
        self.fork = Mock(return_value="fixture-owner/repo")
        self.created_at = "2026-11-21T01:00:00Z"

        def gh_json(*args, **kwargs):
            if "search/issues" in args:
                return {"total_count": 0, "incomplete_results": False, "items": []}
            if args[:2] == ("pr", "view"):
                return {"createdAt": self.created_at} if self.created_at else None
            if is_pulls_read(args):
                return rest_pulls(self.prs)
            return self.prs

        for mocker in (patch.object(submit, "gh_json", side_effect=gh_json),
                       patch.object(submit, "git", side_effect=git), patch.object(submit, "sh", self.process),
                       patch.object(submit, "preconditions", return_value=None),
                       patch.object(submit, "target_privacy", self.target), patch.object(submit, "push_privacy", self.push_scan),
                       patch.object(submit, "ensure_fork", self.fork)):
            mocker.start()
            self.addCleanup(mocker.stop)

    def no_writes(self, before):
        self.assertFalse(any(args[0] == "push" or args[:2] == ("remote", "add") for args in self.commands), self.commands)
        self.assertEqual(self.state(), before)
        self.fork.assert_not_called()
        self.assertFalse(any(call.args[0][:1] == ["gh"] for call in self.process.call_args_list))

    def test_existing_pr_never_renders_or_rewrites_disclosure(self):
        original = self.item.file("pr.md").read_bytes()
        with patch.object(disclosure, "new_pr_body", side_effect=AssertionError("old PR body must not change")):
            result = submit.run(self.item, dry_run=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.item.file("pr.md").read_bytes(), original)
        self.assertFalse(any(call.args[0][0] == "gh" for call in self.process.call_args_list))

    def test_negative_ai_attestations_are_blocked_before_submit(self):
        self.prs.clear()
        for text in ("- [ ] No part of this contribution was generated by AI.",
                     "- [ ] I never used AI to prepare this contribution."):
            with self.subTest(text=text):
                flags, formats = policy.automation_rules({".github/PULL_REQUEST_TEMPLATE.md": text})
                write_json(self.item.file("policy.json"), {"version": policy.VERSION, "at": iso(), "flags": flags,
                                                         "disclosure_formats": formats})
                result = submit.run(self.item, dry_run=True)
                self.assertFalse(result["ok"], result)
                self.assertIn(text, result["report"])
                self.assertFalse(any(args[0] == "push" for args in self.commands))
                self.process.assert_not_called()

    def test_readability_rule_passes_release_gate_and_submission_preview(self):
        self.prs.clear()
        source = "PR descriptions must be human-readable and concise."
        with patch.object(policy, "fetch", side_effect=lambda repo, path: source if path == "CONTRIBUTING.md" else None), \
                patch.object(policy.community, "resolve", return_value={}), \
                patch.object(policy, "gh_json", side_effect=lambda *a: {"default_branch": "main", "stargazers_count": 1,
                    "archived": False} if a[1] == "repos/example/repo" else []):
            scanned = policy.run("example/repo")
        write_json(self.item.file("policy.json"), scanned)
        result = submit.run(self.item, dry_run=True)
        self.assertTrue(result["ok"], result)
        self.assertIn(disclosure.default_line(), result["body"])

    def test_body_edits_keep_disclosure_only_for_post_cutoff_prs(self):
        from factory.core import publication
        outgoing = self.root / "body.txt"
        self.item.meta.update(pr_url=URL, submitted_at="2026-11-01T00:00:00Z")
        self.item.save()
        for created_at, expected in (("2026-11-19T23:59:59Z", "Fixes #2\n"),
                                     ("2026-11-20T00:00:00Z", "Fixes #2\n\n" + disclosure.default_line() + "\n")):
            with self.subTest(created_at=created_at), \
                    patch.object(publication, "gh_json", return_value={"headRefOid": HEAD, "createdAt": created_at}), \
                    patch.object(publication, "release_gate", return_value=[]):
                outgoing.write_text(expected)
                args = ["pr", "edit", URL, "--body-file", str(outgoing)]
                self.assertEqual(publication.gh_release(args, self.clone, self.folder), [])
                outgoing.write_text("Fixes #2\n" if "Prepared by" in expected else
                                    "Fixes #2\n\n" + disclosure.default_line() + "\n")
                self.assertTrue(publication.gh_release(args, self.clone, self.folder))

    def test_body_edits_use_upstream_format_and_recheck_policy_after_gate(self):
        from factory.core import publication
        self.item.meta["pr_url"] = URL
        self.item.save()
        flags, formats = policy.automation_rules({".github/PULL_REQUEST_TEMPLATE.md":
            "- [ ] I used AI assistance for this contribution."})
        scanned = {"version": policy.VERSION, "at": iso(), "flags": flags, "disclosure_formats": formats}
        write_json(self.item.file("policy.json"), scanned)
        outgoing = self.root / "body.txt"
        expected = disclosure.existing_pr_body("Fixes #2\n", scanned, "2026-11-20T00:00:00Z")
        outgoing.write_text(expected)
        args = ["pr", "edit", URL, "--body-file", str(outgoing)]
        with patch.object(publication, "gh_json", return_value={"headRefOid": HEAD, "createdAt": "2026-11-20T00:00:00Z"}):
            with patch.object(publication, "release_gate", return_value=[]):
                self.assertEqual(publication.gh_release(args, self.clone, self.folder), [])
            def refresh(*args):
                write_json(self.item.file("policy.json"), {"version": policy.VERSION, "at": iso(), "flags": {}})
                return []
            with patch.object(publication, "release_gate", side_effect=refresh):
                self.assertTrue(publication.gh_release(args, self.clone, self.folder))
        self.assertIn("- [x] I used AI", expected)
        self.assertIn("https://github.com/" + CONFIG["public"]["repository"], expected)

    def test_body_edit_without_creation_date_refuses_instead_of_removing_disclosure(self):
        from factory.core import publication
        self.item.meta["pr_url"] = URL
        self.item.save()
        outgoing = self.root / "body.txt"
        outgoing.write_text("Fixes #2\n")
        with patch.object(publication, "gh_json", return_value={"headRefOid": HEAD}), \
                patch.object(publication, "release_gate", return_value=[]):
            self.assertTrue(publication.gh_release(["pr", "edit", URL, "--body-file", str(outgoing)],
                                                   self.clone, self.folder))

    def test_existing_pr_dry_run_never_pushes_or_changes_item_or_ledger(self):
        before = self.state()
        with patch.object(submit.gate, "run", wraps=gate.run) as checked:
            result = submit.run(self.item, dry_run=True)
        self.no_writes(before)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["to"], HEAD)
        self.assertEqual(checked.call_args.kwargs, {"with_approval": True})

    def test_existing_pr_dry_run_is_read_only_on_noop_and_refusal_paths(self):
        for scenario in ("CLOSED", "MERGED", "current", "missing-fork", "gate-failed", "moved-remote"):
            with self.subTest(scenario=scenario):
                self.prs[0]["state"] = scenario if scenario in ("CLOSED", "MERGED") else "OPEN"
                self.remote = HEAD if scenario == "current" else OLD
                self.remotes = "origin" if scenario == "missing-fork" else "origin\nfork"
                self.item.meta["pushed_head"] = "c" * 40 if scenario == "moved-remote" else OLD
                self.item.save()
                self.process.return_value.returncode = 1 if scenario == "moved-remote" else 0
                write_json(self.item.file("coordinator-review.json"), {})
                self.item.file("coordinator-review.md").write_text(f"head: {HEAD}\nverdict: {'drop' if scenario == 'gate-failed' else 'ship'}\n")
                before = self.state()
                result = submit.run(self.item, dry_run=True)
                self.assertEqual(result["ok"], scenario in ("CLOSED", "MERGED", "current"), result)
                self.assertTrue(result["dry_run"])
                self.no_writes(before)

    def test_new_pr_dry_run_is_read_only_when_gate_passes_or_fails(self):
        self.prs.clear()
        for approved in (True, False):
            with self.subTest(approved=approved):
                if not approved:
                    self.item.file("approval.json").unlink()
                before = self.state()
                result = submit.run(self.item, dry_run=True)
                self.assertEqual(result["ok"], approved, result)
                self.no_writes(before)

    def test_existing_update_rejects_missing_coordinator_before_push(self):
        self.item.file("coordinator-review.md").unlink()
        result = submit.run(self.item)
        self.assertFalse(result["ok"])
        self.assertIn("coordinator review", result["report"])
        self.assertFalse(any(args[0] == "push" for args in self.commands))
        self.target.assert_not_called()
        self.assertEqual(ledger.load()[0]["url"], URL)

    def test_successful_existing_update_recovers_the_ledger(self):
        result = submit.run(self.item)
        self.assertTrue(result["updated"], result)
        self.assertIn(("push", "fork", f"{HEAD}:refs/heads/contrib/parser"), self.commands)
        self.assertEqual(ledger.load()[0]["url"], URL)
        self.assertEqual(Item(self.folder).meta["pushed_head"], HEAD)

    def test_existing_pr_recovery_inserts_missing_row_even_on_noop(self):
        self.remote = HEAD
        result = submit.run(self.item)
        self.assertTrue(result["ok"], result)
        row = ledger.find(ledger.load(), URL)
        self.assertEqual((row["number"], row["repo"], row["item"]), (7, "example/repo", str(self.folder)))
        self.assertEqual(row["submitted_at"], self.prs[0]["createdAt"])
        row.update(status="in-review", lesson="L-001", notes="Keep this note", records={"profile": {"ref": "saved"}})
        write_json(self.ledger, [row])
        before = self.ledger.read_bytes()
        self.assertTrue(submit.run(self.item)["ok"])
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_recovered_closed_and_merged_rows_keep_observed_outcome(self):
        for state in ("CLOSED", "MERGED"):
            with self.subTest(state=state):
                write_json(self.ledger, [])
                self.prs[0].update(state=state, mergedAt="2026-11-02T00:00:00Z" if state == "MERGED" else None)
                self.assertTrue(submit.run(self.item)["ok"])
                row = ledger.load()[0]
                self.assertEqual(row["status"], state.lower())
                if state == "MERGED":
                    self.assertEqual(row["merged_at"], self.prs[0]["mergedAt"])

    def test_changed_inputs_after_gate_stop_new_and_existing_publications(self):
        original = self.item.file("pr.md").read_bytes()
        for existing in (False, True):
            for changed in ("head", "text"):
                with self.subTest(existing=existing, changed=changed):
                    self.prs[:] = [{"url": URL, "state": "OPEN"}] if existing else []
                    self.remote = OLD
                    self.item.file("pr.md").write_bytes(original)
                    self.commands.clear()
                    current = [HEAD]
                    def move(*args, **kwargs):
                        if changed == "head":
                            current[0] = "c" * 40
                        else:
                            self.item.file("pr.md").write_text("# fix: not reviewed\n")
                        return []
                    self.target.side_effect = move
                    with patch.object(Item, "head_sha", side_effect=lambda: current[0]), \
                            patch.object(qualify, "fresh_collisions", return_value={"verdict": "ok"}):
                        result = submit.run(self.item)
                    self.assertFalse(result["ok"], result)
                    self.assertIn("re-review", result["reason"])
                    self.assertFalse(any(args[0] == "push" for args in self.commands))

    def test_gate_result_and_frozen_approval_must_match(self):
        for existing in (False, True):
            self.prs[:] = [{"url": URL, "state": "OPEN"}] if existing else []
            for mismatch in ("head", "text"):
                with self.subTest(existing=existing, mismatch=mismatch):
                    write_json(self.item.file("approval.json"), {"at": iso(), "head_sha": HEAD,
                        "pr_sha256": "wrong" if mismatch == "text" else verdicts.pr_sha256(self.item)})
                    with patch.object(gate, "run", return_value=(Mock(ok=True), {"head_sha": OLD if mismatch == "head" else HEAD})):
                        result = submit.run(self.item)
                    self.assertFalse(result["ok"], result)
                    self.assertIn("re-review", result["reason"])
                    self.target.assert_not_called()

    def test_pr_text_change_during_preconditions_stops_before_fork(self):
        self.prs.clear()
        def moved(item):
            item.file("pr.md").write_text("# fix: unpublished edit\n")
        with patch.object(submit, "preconditions", side_effect=moved):
            result = submit.run(self.item)
        self.assertFalse(result["ok"], result)
        self.assertIn("re-review", result["reason"])
        self.fork.assert_not_called()

    def test_new_pr_records_github_creation_time(self):
        self.prs.clear()
        self.created_at = "2026-11-21T15:59:59Z"
        with patch.object(qualify, "fresh_collisions", return_value={"verdict": "ok"}):
            result = submit.run(self.item)
        self.assertTrue(result.get("url"), result)
        self.assertEqual(self.item.meta["submitted_at"], "2026-11-21T15:59:59Z")
        row = submit.ledger.find(submit.ledger.load(), URL)
        self.assertEqual(row["submitted_at"], "2026-11-21T15:59:59Z")

    @contextlib.contextmanager
    def cross_midnight_creation(self):
        self.prs.clear()
        current = ["2026-11-21T15:59:59Z"]
        read_created_at = Mock(side_effect=submit.CommandError("createdAt temporarily unavailable"))

        def create(args):
            self.assertEqual(args[:3], ["gh", "pr", "create"])
            current[0] = "2026-11-21T16:00:01Z"
            return Mock(returncode=0, stdout=URL)

        def lookup(*args, **kwargs):
            if "search/issues" in args:
                return {"total_count": 0, "incomplete_results": False, "items": []}
            if is_pulls_read(args):
                return rest_pulls(self.prs)
            if args[:2] == ("pr", "view"):
                return read_created_at()
            self.fail(f"unexpected GitHub read: {args}")

        self.process.side_effect = create
        with patch.object(submit, "gh_json", side_effect=lookup), \
                patch.object(submit, "iso", side_effect=lambda: current[0]), \
                patch.object(submit.time, "sleep"), \
                patch.object(qualify, "fresh_collisions", return_value={"verdict": "ok"}):
            yield read_created_at

    def test_cross_midnight_unreadable_created_at_keeps_url_without_guessing(self):
        with self.cross_midnight_creation():
            result = submit.run(self.item)
        creates = [call for call in self.process.call_args_list if call.args[0][:3] == ["gh", "pr", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertEqual(result.get("url"), URL)
        row = ledger.find(ledger.load(), URL)
        with self.subTest(evidence="creation time"):
            if row is not None:
                self.assertEqual(row.get("submitted_at"), "2026-11-21T15:59:59Z",
                                 "the local reply time is not verified GitHub creation time")
        with self.subTest(evidence="result"):
            self.assertFalse(result["ok"], "an unreadable creation time must not be reported as silent success")

    def test_cross_midnight_retry_recovers_creation_time_once_without_recreating(self):
        with self.cross_midnight_creation():
            submit.run(self.item)
            self.prs[:] = [{"url": URL, "state": "OPEN", "createdAt": "2026-11-21T15:59:59Z"}]
            for _ in range(2):
                result = submit.run(Item(self.folder))
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["url"], URL)
        creates = [call for call in self.process.call_args_list if call.args[0][:3] == ["gh", "pr", "create"]]
        self.assertEqual(len(creates), 1, "retry must recover the existing PR, not create another")
        rows = [row for row in ledger.load() if row["url"] == URL]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["submitted_at"], "2026-11-21T15:59:59Z")
        self.assertEqual(ledger.opened_on(parse_iso("2026-11-21T23:59:59+08:00").date()), {("example/repo", 7)})
        self.assertEqual(ledger.opened_on(parse_iso("2026-11-22T00:00:05+08:00").date()), set())

    def test_cross_midnight_transient_created_at_failure_retries_within_submit(self):
        with self.cross_midnight_creation() as read_created_at:
            read_created_at.side_effect = [submit.CommandError("createdAt temporarily unavailable"),
                                          {"createdAt": "2026-11-21T15:59:59Z"}]
            result = submit.run(self.item)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["url"], URL)
        self.assertEqual(ledger.find(ledger.load(), URL)["submitted_at"], "2026-11-21T15:59:59Z")
        self.assertEqual(self.item.meta["submitted_at"], "2026-11-21T15:59:59Z")
        self.assertEqual(read_created_at.call_count, 2)
        creates = [call for call in self.process.call_args_list if call.args[0][:3] == ["gh", "pr", "create"]]
        self.assertEqual(len(creates), 1)

    @contextlib.contextmanager
    def crashed_pr_creation(self, crash_at="item save"):
        """Creation succeeds remotely, but the process dies before saving the item or ledger."""
        created_at = "2026-11-22T03:00:00Z"
        write_json(self.ledger, [])
        remote_prs = {}

        def lookup(*args, **kwargs):
            if args[:2] == ("search", "prs"):
                return []
            if args[:2] == ("pr", "list"):
                return []  # gh pr list with --author answers from the search index, which has not listed the PR yet
            if is_pulls_read(args):
                repo = args[3][len("repos/"):-len("/pulls")]
                branch = next(arg for arg in args if arg.startswith("head=")).split(":", 1)[1]
                return rest_pulls([remote_prs[repo, branch]]) if (repo, branch) in remote_prs else []
            if args[:2] == ("pr", "view"):
                return {"createdAt": created_at}
            self.fail(f"unexpected GitHub read: {args}")

        def create(args):
            self.assertEqual(args[:3], ["gh", "pr", "create"])
            repo = args[args.index("-R") + 1]
            branch = args[args.index("--head") + 1].split(":", 1)[1]
            url = f"https://github.com/{repo}/pull/7"
            remote_prs[repo, branch] = {"url": url, "state": "OPEN", "createdAt": created_at}
            return Mock(returncode=0, stdout=url)

        save = self.item.save
        def crash_after_create():
            if remote_prs:
                raise RuntimeError("simulated crash before item save")
            save()

        self.process.side_effect = create
        with patch.object(submit, "iso", return_value=created_at), \
                patch.object(submit, "gh_json", side_effect=lookup), \
                patch.object(submit, "preconditions", wraps=submit_preconditions), \
                patch.object(qualify, "fresh_collisions", return_value={"verdict": "ok"}):
            if crash_at == "item save":
                crash = patch.object(self.item, "save", side_effect=crash_after_create)
            else:
                crash = patch.object(submit.ledger, "upsert", side_effect=RuntimeError("simulated crash before ledger write"))
            with crash, self.assertRaisesRegex(RuntimeError, "simulated crash before"):
                submit.run(self.item)
            self.assertEqual(len(remote_prs), 1)
            self.assertEqual(ledger.load(), [])
            self.assertIsNone(ledger.find(ledger.load(), URL))
            # Discard the interrupted process's mutated in-memory metadata.
            self.item = Item(self.folder)
            yield

    def test_module_isolation_drops_an_inherited_run_start(self):
        with patch.dict(os.environ, {shiftlock.RUN_STARTED: SHIFT_STARTED}), isolated_config():
            self.assertNotIn(shiftlock.RUN_STARTED, os.environ)

    def test_retry_after_crash_recovers_the_pr_without_creating_it_again(self):
        for crash_at in ("item save", "ledger write"):
            with self.subTest(crash_at=crash_at):
                self.process.reset_mock()
                with self.crashed_pr_creation(crash_at):
                    result = submit.run(self.item)
                creates = [call for call in self.process.call_args_list if call.args[0][:3] == ["gh", "pr", "create"]]
                self.assertEqual(len(creates), 1, "the retry must find the PR in the repository list, not create it again")
                self.assertTrue(result["ok"], result)
                row = ledger.find(ledger.load(), URL)
                self.assertEqual(row["submitted_at"], "2026-11-22T03:00:00Z")

    def test_branch_prs_reads_our_fork_branch_from_the_repository_list(self):
        me = CONFIG["github_user"]
        replies = rest_pulls([{"url": URL, "state": "MERGED", "createdAt": "2026-11-22T03:00:00Z", "mergedAt": "2026-11-23T00:00:00Z"},
                              {"url": URL.replace("/7", "/6"), "state": "CLOSED", "createdAt": "2026-11-20T03:00:00Z"}])
        calls = []
        def lookup(*args, **kwargs):
            calls.append(args)
            return replies
        with patch.object(submit, "gh_json", side_effect=lookup):
            prs = submit.branch_prs("example/repo", me, "contrib/parser")
        self.assertEqual(prs, [
            {"url": URL, "state": "MERGED", "createdAt": "2026-11-22T03:00:00Z", "mergedAt": "2026-11-23T00:00:00Z"},
            {"url": URL.replace("/7", "/6"), "state": "CLOSED", "createdAt": "2026-11-20T03:00:00Z", "mergedAt": None}])
        call, = calls
        self.assertEqual(call[:4], ("api", "-X", "GET", "repos/example/repo/pulls"))
        self.assertIn(f"head={me}:contrib/parser", call, "the head filter keeps other authors' same-named branches out")
        self.assertIn("state=all", call)
        self.assertNotIn("--author", call, "gh answers --author from the lagging search index")

    def test_list_entry_without_a_creation_time_key_still_recovers_through_pr_view(self):
        self.prs[:] = [{"url": URL, "state": "OPEN"}]
        self.created_at = "2026-11-21T15:59:59Z"
        reply = [{key: value for key, value in entry.items() if key != "created_at"} for entry in rest_pulls(self.prs)]
        with patch.object(submit, "gh_json", side_effect=lambda *a, **k: reply if is_pulls_read(a) else {"createdAt": self.created_at}):
            self.assertEqual(submit.branch_prs("example/repo", CONFIG["github_user"], "contrib/parser")[0]["createdAt"], None)
            submit.recover_existing(self.item, submit.branch_prs("example/repo", CONFIG["github_user"], "contrib/parser")[0])
        self.assertEqual(ledger.find(ledger.load(), URL)["submitted_at"], "2026-11-21T15:59:59Z")

    def test_recovery_without_github_creation_time_preserves_url_without_guessing_or_recreating(self):
        self.prs[:] = [{"url": URL, "state": "OPEN", "createdAt": None}]
        self.created_at = None
        self.item.file("submit-qualify.json").write_text('{"verdict": "ok"}')
        with patch.object(submit.time, "sleep"):
            submit.run(self.item)
        self.assertEqual(Item(self.folder).meta.get("pr_url"), URL)
        self.assertIsNone(ledger.find(ledger.load(), URL), "no local time stands in for GitHub's creation time")
        self.assertFalse(any(call.args[0][:3] == ["gh", "pr", "create"] for call in self.process.call_args_list))

    def test_new_pr_uses_frozen_text_and_checks_remote_head(self):
        self.prs.clear()
        def create(args):
            self.assertEqual(pathlib.Path(args[args.index("--body-file") + 1]).read_text(),
                             "Fixes #2\n\n" + disclosure.default_line() + "\n")
            self.item.file("pr.md").write_text("# fix: concurrent edit\n")
            return Mock(stdout=URL)
        self.process.side_effect = create
        with patch.object(qualify, "fresh_collisions", return_value={"verdict": "ok"}):
            result = submit.run(self.item)
        self.assertFalse(result["ok"], result)
        self.assertIn("re-review", result["reason"])
        self.assertIn(("push", "fork", f"{HEAD}:refs/heads/contrib/parser"), self.commands)
        self.assertGreaterEqual(sum(args[0] == "ls-remote" for args in self.commands), 2)

    def test_push_uses_frozen_sha_even_if_branch_moves_at_the_write_boundary(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.prs[:] = [{"url": URL, "state": "OPEN"}] if existing else []
                self.remote = OLD
                self.commands.clear()
                current = [HEAD]
                git = submit.git
                def move(clone, *args, **kwargs):
                    if args[0] == "push":
                        current[0] = "c" * 40
                    return git(clone, *args, **kwargs)
                with patch.object(Item, "head_sha", side_effect=lambda: current[0]), patch.object(submit, "git", side_effect=move):
                    result = submit.run(self.item)
                self.assertFalse(result["ok"], result)
                self.assertIn("re-review", result["reason"])
                self.assertIn(("push", "fork", f"{HEAD}:refs/heads/contrib/parser"), self.commands)
                self.assertEqual(self.target.call_args.kwargs, {"head": HEAD})
                self.assertEqual(self.push_scan.call_args.args[1], ["fork", f"{HEAD}:refs/heads/contrib/parser"])

    def test_remote_head_mismatch_after_push_is_not_reported_successful(self):
        self.remote = OLD
        def wrong_remote(clone, *args, **kwargs):
            self.commands.append(args)
            if args == ("remote",):
                return "origin\nfork"
            if args[0] == "ls-remote":
                return OLD + "\trefs/heads/contrib/parser"
            return ""
        with patch.object(submit, "git", side_effect=wrong_remote):
            result = submit.run(self.item)
        self.assertFalse(result["ok"], result)
        self.assertIn("remote", result["reason"])
        self.assertIn("re-review", result["reason"])
        self.assertEqual(Item(self.folder).meta["pushed_head"], OLD)

    def test_dry_run_only_refreshes_named_cache_files(self):
        for existing in (False, True):
            for scenario in ("direct", "stacked", "renamed", "issue-first"):
                with self.subTest(existing=existing, scenario=scenario):
                    self.prs[:] = [{"url": URL, "state": "OPEN"}] if existing else []
                    self.item.meta.update(repo="example/renamed" if scenario == "renamed" else "example/repo",
                                          stacked_on="contrib/base" if scenario == "stacked" else None,
                                          issue_first="issue" if scenario == "issue-first" else "direct")
                    self.item.save()
                    for name in ("policy.json", "qualify.json"):
                        self.item.file(name).unlink(missing_ok=True)
                    before, ledger_before = self.snapshot(), self.ledger.read_bytes()
                    with patch.object(policy, "run", return_value={"version": policy.VERSION, "at": iso(), "flags": {}}), \
                            patch.object(qualify, "run", return_value={"at": iso(), "verdict": "ok"}):
                        result = submit.run(self.item, dry_run=True)
                    self.assertTrue(result["ok"], result)
                    self.assert_cache_changes_only(before)
                    self.assertEqual(self.ledger.read_bytes(), ledger_before)
                    self.assertFalse(any(args[0] == "push" for args in self.commands))
        self.fork.assert_not_called()


class ShipCoordinator(ReleaseCase):
    def setUp(self):
        super().setUp()
        self.send = Mock(side_effect=AssertionError("no PR should be sent"))
        self.write_issue = Mock(side_effect=AssertionError("no issue should be sent"))
        self.item.file("approval.json").unlink()
        for mocker in (patch.object(ship, "linked_worktree", return_value=False),
                       patch.object(submit, "preconditions", return_value=None),
                       patch.object(submit, "run", self.send), patch.object(cli, "cmd_open_issue", self.write_issue)):
            mocker.start()
            self.addCleanup(mocker.stop)

    def test_missing_coordinator_refuses_before_issue_including_dry_run(self):
        self.item.meta.update(issue=None, issue_first="issue")
        self.item.save()
        self.item.file("coordinator-review.md").unlink()
        before = self.state()
        for dry_run in (False, True):
            result = ship.run(str(self.folder), dry_run=dry_run)
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["steps"][-1]["name"], "coordinator")
            self.assertIn("coordinator-review.md", result["steps"][-1]["detail"])
            self.assertEqual(self.state(), before)
        self.send.assert_not_called()
        self.write_issue.assert_not_called()
        self.assertFalse(self.item.file("approval.json").exists())

    def test_dry_run_with_and_without_issue_leaves_state_and_approvals_unchanged(self):
        for schema in (1, 2):
            if schema == 2:
                self.schema2()
            for needs_issue in (False, True):
                with self.subTest(schema=schema, needs_issue=needs_issue):
                    self.item.meta.update(issue=None if needs_issue else 2, issue_first="issue" if needs_issue else "direct")
                    self.item.save()
                    self.item.file("pr.md").write_text("# fix: retain parsed titles\n\nFixes #ISSUE\n" if needs_issue else "# fix: retain parsed titles\n\nFixes #2\n")
                    self.item.file("issue.md").write_text("# Parser loses titles\n\nParse a named field.\n")
                    if schema == 2:
                        review = read_json(self.item.file("coordinator-review.json"))
                        write_json(self.item.file("coordinator-review.json"), {**review, "pr_sha256": verdicts.pr_sha256(self.item)})
                    for name in ("policy.json", "qualify.json"):
                        self.item.file(name).unlink(missing_ok=True)
                    before, files_before = self.state(), self.snapshot()
                    with patch.object(policy, "run", return_value={"version": policy.VERSION, "at": iso(), "flags": {}}), \
                            patch.object(qualify, "run", return_value={"at": iso(), "verdict": "ok"}):
                        result = ship.run(str(self.folder), dry_run=True)
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["steps"][-1]["name"], "issue" if needs_issue else "clean")
                    self.assertEqual(self.state(), before)
                    self.assert_cache_changes_only(files_before)
                    self.assertFalse(self.item.file("approval.json").exists())
                    self.assertFalse(self.item.file("approval-issue.json").exists())
        self.send.assert_not_called()
        self.write_issue.assert_not_called()


class PlanBuckets(ReleaseCase):
    def test_ready_requires_coordinator_ship_for_the_current_head(self):
        self.item.file("coordinator-review.md").unlink()
        self.assertEqual(plan.classify(self.item)[0], "needs_coordinator_review")
        report = plan.run()
        self.assertEqual(report["ship_ready"], [])
        self.assertEqual(len(report["needs_coordinator_review"]), 1)
        for head, verdict, bucket in ((OLD, "ship", "needs_coordinator_review"), (HEAD, "drop", "needs_coordinator_review"),
                                       (HEAD, "ship", "ship_ready")):
            self.item.file("coordinator-review.md").write_text(f"head: {head}\nverdict: {verdict}\n")
            self.assertEqual(plan.classify(self.item)[0], bucket)

    def test_schema2_value_review_build_precedes_review_and_coordinator_buckets(self):
        self.schema2()
        for verdict in (None, "propose", "drop", "ship"):
            with self.subTest(verdict=verdict):
                write_json(self.item.file("value-review.json"), {"verdict": verdict})
                bucket, entry = plan.classify(self.item)
                self.assertEqual(bucket, "held")
                self.assertEqual(entry["reasons"], protocol.validate_value(self.item))
        write_json(self.item.file("value-review.json"), {"verdict": "build"})
        self.assertEqual(plan.classify(self.item)[0], "held")
        self.schema2()
        self.assertEqual(plan.classify(self.item)[0], "ship_ready")
        self.item.file("coordinator-review.json").unlink()
        self.assertEqual(plan.classify(self.item)[0], "needs_coordinator_review")
        self.item.file("review.json").unlink()
        self.assertEqual(plan.classify(self.item)[0], "unreviewed")
        self.item.file("value-review.json").unlink()
        self.assertEqual(plan.classify(self.item)[0], "needs_value_review")

    def test_schema2_coordinator_hash_and_demand_are_required(self):
        self.schema2()
        review = read_json(self.item.file("coordinator-review.json"))
        write_json(self.item.file("coordinator-review.json"), {**review, "pr_sha256": None})
        self.assertEqual(plan.classify(self.item)[0], "needs_coordinator_review")
        self.item.file("demand.json").unlink()
        self.assertEqual(plan.classify(self.item)[0], "held")


class ReleaseEvidence(ReleaseCase):
    def test_target_privacy_scans_the_frozen_sha_not_the_current_branch(self):
        base = "d" * 40
        with patch.object(Item, "head_sha", return_value=OLD), \
                patch.object(submit, "git", side_effect=lambda clone, *args: base if args[0] in ("merge-base", "rev-parse") else "") as git, \
                patch.object(submit.publication, "context", return_value=(None, None, {})), \
                patch.object(submit.published, "on_pr_branch", return_value=frozenset()), \
                patch.object(submit.privacy_git, "scan", return_value=[]) as scan:
            self.assertEqual(submit.target_privacy(self.item, [], head=HEAD), [])
        git.assert_any_call(self.clone, "merge-base", base, HEAD)
        self.assertEqual(scan.call_args.args, (self.clone, [HEAD, "^" + base]))

    def test_schema2_issue_link_keeps_reviews_but_requires_new_raw_approval(self):
        self.item.meta.update(issue=None, issue_first="issue")
        self.item.file("pr.md").write_text("# fix: retain titles\n\nFixes #ISSUE\n")
        self.schema2()
        write_json(self.item.file("approval.json"), {"at": iso(), "head_sha": HEAD, "pr_sha256": verdicts.pr_sha256(self.item)})
        self.extra.side_effect = RUN_EXTRA
        initial, _ = gate.run(self.item, mode="release")
        self.assertTrue(initial.ok, initial.text())
        self.item.meta["issue"] = 12
        self.item.save()
        self.item.file("pr.md").write_text("# fix: retain titles\n\nFixes #12\n")
        rep, _ = gate.run(self.item, mode="release")
        for name in ("coordinator review", "review-obligations", "coordinator-proof"):
            self.assertTrue(self.row(rep, name)["ok"], rep.text())
        self.assertFalse(self.row(rep, "human approval")["ok"])
        self.assertEqual(plan.classify(self.item)[0], "ship_ready")

    def test_pre_issue_keeps_all_non_issue_release_checks(self):
        self.schema2()
        self.extra.side_effect = RUN_EXTRA
        self.item.meta.update(issue=None, issue_first="issue")
        self.item.file("approval.json").unlink()
        write_json(self.item.file("policy.json"), {"version": policy.VERSION, "at": iso(), "flags": {
            "linked_issue_enforced": ["Fixes keyword and assigned issue required"], "repro_required": ["Reproduce before fixing"]}})
        rep, result = gate.pre_issue(self.item)
        self.assertTrue(rep.ok, rep.text())
        self.assertEqual(result["mode"], "release")
        names = {row["check"] for row in rep.rows}
        self.assertTrue({"independent review", "coordinator review", "coordinator-proof"} <= names)
        self.assertFalse(names & gate.PRE_ISSUE_EXCLUDED)
        self.extra.side_effect = lambda item, rep, mode: rep.add("required release evidence", False, "run the check")
        self.assertFalse(gate.pre_issue(self.item)[0].ok)

    def test_schema2_issue_first_ship_keeps_review_proofs_and_approves_final_text(self):
        self.item.meta.update(issue=None, issue_first="issue", target="none")
        self.item.file("pr.md").write_text("# fix: retain titles\n\nFixes #ISSUE\n")
        self.item.file("issue.md").write_text("# Parser loses titles\n\nParse a named field.\n")
        self.schema2()
        self.extra.side_effect = RUN_EXTRA
        self.item.file("approval.json").unlink()
        review_before = self.item.file("review.json").read_bytes()
        coordinator_before = self.item.file("coordinator-review.json").read_bytes()
        def send(item):
            rep, _ = gate.run(item, mode="release")
            self.assertTrue(rep.ok, rep.text())
            self.assertEqual(read_json(item.file("approval.json"))["pr_sha256"], verdicts.pr_sha256(item))
            self.assertIn("Fixes #12", item.file("pr.md").read_text())
            return {"ok": True, "url": URL}
        with patch.object(ship, "linked_worktree", return_value=False), \
                patch.object(submit, "preconditions", return_value=None), patch.object(submit, "run", side_effect=send) as sent, \
                patch.object(cli, "git", return_value="1"), patch.object(cli, "publication_findings", return_value=([], [])), \
                patch.object(cli, "gh_json", return_value=[]), \
                patch.object(cli, "run", return_value=Mock(stdout="https://github.com/example/repo/issues/12\n")), \
                patch.object(qualify, "run", return_value={"at": iso(), "verdict": "ok"}), \
                patch.object(ship.Ship, "verify", return_value="verified"), patch.object(ship.Ship, "tidy", return_value="clean"):
            result = ship.run(str(self.folder))
        self.assertTrue(result["ok"], result)
        sent.assert_called_once()
        self.assertEqual(self.item.file("review.json").read_bytes(), review_before)
        self.assertEqual(self.item.file("coordinator-review.json").read_bytes(), coordinator_before)

    def test_gate_reports_unreadable_evidence_without_raising(self):
        self.schema2()
        self.extra.side_effect = RUN_EXTRA
        for name in ("value-review.json", "review.json", "coordinator-review.json"):
            original = self.item.file(name).read_bytes()
            for text in ("{", "[]"):
                with self.subTest(name=name, text=text):
                    self.item.file(name).write_text(text)
                    rep, result = gate.run(self.item, mode="release")
                    self.assertFalse(result["ok"])
                    self.assertIn(name, rep.text())
            self.item.file(name).write_bytes(original)
        self.extra.side_effect = ValueError("review.json: invalid evidence")
        rep, _ = gate.run(self.item, mode="release")
        self.assertFalse(rep.ok)
        self.assertIn("review.json", rep.text())

    def test_plan_validates_value_review_and_coordinator_structure(self):
        self.schema2()
        for name, bucket in (("value-review.json", "held"), ("review.json", "held"),
                             ("coordinator-review.json", "needs_coordinator_review")):
            with self.subTest(name=name):
                original = self.item.file(name).read_bytes()
                data = ({"verdict": "build", "demand_sha256": verdicts.demand_sha256(self.item)} if name == "value-review.json" else
                        {"verdict": "ship", "head": HEAD, "pr_sha256": verdicts.pr_text_sha256(self.item)})
                write_json(self.item.file(name), data)
                actual, entry = plan.classify(self.item)
                self.assertEqual(actual, bucket, entry)
                self.assertTrue(entry["reasons"])
                self.item.file(name).write_bytes(original)
        write_json(self.item.file("review.json"), {**read_json(self.item.file("review.json")), "reviewer": "builder"})
        self.assertEqual(plan.classify(self.item)[0], "held")

    def test_plan_marks_changed_review_text_stale_even_with_new_coordinator_hash(self):
        self.schema2()
        self.item.file("pr.md").write_text("# fix: updated description\n")
        self.assertEqual(protocol.validate_value(self.item), [])
        write_json(self.item.file("coordinator-review.json"), {
            **read_json(self.item.file("coordinator-review.json")), "pr_sha256": verdicts.pr_text_sha256(self.item)})
        bucket, entry = plan.classify(self.item)
        self.assertEqual(bucket, "stale_reviews", entry)
        self.assertIn("pr_sha256", " ".join(entry["reasons"]))

    def test_plan_isolates_unreadable_files_and_validator_value_errors(self):
        self.schema2()
        other = self.folder.with_name("other")
        other.mkdir()
        for path in self.folder.iterdir():
            if path.is_file():
                (other / path.name).write_bytes(path.read_bytes())
        for name in ("value-review.json", "review.json", "coordinator-review.json"):
            original = self.item.file(name).read_bytes()
            for text in ("{", "[]"):
                with self.subTest(name=name, text=text):
                    self.item.file(name).write_text(text)
                    report = plan.run()
                    if name == "value-review.json":
                        self.assertEqual(report["errors"], [])
                        self.assertEqual(report["held"][0]["reasons"], protocol.validate_value(self.item))
                    else:
                        self.assertEqual(len(report["errors"]), 1, report)
                        self.assertIn(name, report["errors"][0]["error"])
                    self.assertEqual(len(report["ship_ready"]), 1)
            self.item.file(name).write_bytes(original)
        for name, validator in (("value-review.json", "validate_value"), ("review.json", "validate_review"),
                                ("coordinator-review.json", "validate_coordinator")):
            validate = getattr(protocol, validator)
            def broken(item):
                if item.dir == self.folder:
                    raise ValueError("invalid field")
                return validate(item)
            with self.subTest(validator=validator), patch.object(protocol, validator, side_effect=broken):
                report = plan.run()
                self.assertIn(name, report["errors"][0]["error"])
                self.assertEqual(len(report["ship_ready"]), 1)

    def test_open_issue_requires_full_release_checks_except_before_fix(self):
        self.schema2()
        self.extra.side_effect = RUN_EXTRA
        self.item.file("issue.md").write_text("# Parser loses titles\n\nParse a named field.\n")
        self.cli("approve-proposal", str(self.folder), "--file", "issue.md", "--note", "Approved discussion.")
        self.item.file("approval.json").unlink()
        for name in ("review.json", "coordinator-review.json"):
            original = self.item.file(name).read_bytes()
            self.item.file(name).unlink()
            with self.subTest(name=name), patch.object(cli, "git", return_value="1"), \
                    patch.object(cli, "gh_json") as lookup, patch.object(cli, "run") as outward:
                with self.assertRaisesRegex(SystemExit, "review"):
                    self.cli("open-issue", str(self.folder))
                outward.assert_not_called()
                lookup.assert_not_called()
            self.item.file(name).write_bytes(original)
        write_json(self.item.file("coordinator-review.json"), {
            "head": HEAD, "verdict": "ship", "pr_sha256": verdicts.pr_text_sha256(self.item)})
        with patch.object(cli, "git", return_value="1"), patch.object(cli, "gh_json", return_value=[]) as lookup, \
                patch.object(cli, "run", return_value=Mock(stdout="https://github.com/example/repo/issues/12\n")) as outward:
            with self.assertRaisesRegex(SystemExit, "coordinator-proof"):
                self.cli("open-issue", str(self.folder))
            lookup.assert_not_called()
            outward.assert_not_called()
            self.cli("open-issue", str(self.folder), "--before-fix")
            outward.assert_called_once()

    def test_ship_checks_full_coordinator_proof_before_issue_and_dry_run(self):
        self.schema2()
        self.extra.side_effect = RUN_EXTRA
        write_json(self.item.file("coordinator-review.json"), {
            "head": HEAD, "verdict": "ship", "pr_sha256": verdicts.pr_text_sha256(self.item)})
        for needs_issue in (False, True):
            self.item.meta.update(issue=None if needs_issue else 2, issue_first="issue" if needs_issue else "direct")
            self.item.save()
            for dry_run in (False, True):
                with self.subTest(issue=needs_issue, dry=dry_run), patch.object(ship, "linked_worktree", return_value=False), \
                        patch.object(submit, "preconditions", return_value=None), patch.object(submit, "run") as send, \
                        patch.object(cli, "cmd_open_issue") as open_issue:
                    result = ship.run(str(self.folder), dry_run=dry_run)
                    self.assertFalse(result["ok"], result)
                    self.assertEqual(result["steps"][-1]["name"], "coordinator")
                    self.assertIn("coordinator-proof", result["steps"][-1]["detail"])
                    send.assert_not_called()
                    open_issue.assert_not_called()


class CliMetadata(ReleaseCase):
    def test_new_schema2_qualifies_discussions_without_template_path_shadowing(self):
        templates = self.root / "lines" / "oss" / "templates"
        templates.mkdir(parents=True)
        (templates / "verify.md").write_text("verdict: fix-needed\n")
        for ref, args in (("example/repo#12", ["--discussion"]), ("https://github.com/example/repo/discussions/12", [])):
            with self.subTest(ref=ref), patch.object(cli, "ROOT", self.root), patch.object(cli, "clone_dir", return_value=self.clone), \
                    patch.object(cli, "git", return_value="contrib/parser"), patch.object(qualify, "run", return_value={"verdict": "ok", "skip": []}) as qualified, \
                    patch.object(policy, "run", return_value={}):
                self.assertEqual(self.cli("new", ref, "--slug", "parser", "--base", "main", *args), 0)
                qualified.assert_called_once_with("example/repo", 12, [], "discussion")
        metadata = next(path for path in self.runs.glob("*/*/item.json") if path.parent.name == "repo-12-parser")
        data = read_json(metadata)
        self.assertEqual((data["schema"], data["target"], data["required_checks"], data["tags"]), (2, "discussion", [], []))
        self.assertTrue(metadata.with_name("verify.md").exists())

    def test_approval_records_standing_prefix_or_explicit_user_note(self):
        standing = CONFIG["standing_approval_note"]
        for note, by in ((standing, "standing"), (standing + " Reviewed this parser.", "standing"),
                         ("Approved this head.", "user"), ("Quote: " + standing, "user")):
            with self.subTest(note=note):
                self.assertEqual(self.cli("approve", str(self.folder), "--note", note), 0)
                approval = read_json(self.item.file("approval.json"))
                self.assertEqual((approval["by"], approval["note"]), (by, note))
                self.assertEqual((approval["head_sha"], approval["pr_sha256"]), (HEAD, verdicts.pr_sha256(self.item)))
        with patch.dict(CONFIG, standing_approval_note=""):
            self.cli("approve", str(self.folder), "--note", "Approved this head.")
        self.assertEqual(read_json(self.item.file("approval.json"))["by"], "user")

    def test_standing_note_is_compared_after_trimming_both_sides(self):
        for standing in (" Standing permission. ", "Standing permission.\n"):
            with self.subTest(standing=standing), patch.dict(CONFIG, standing_approval_note=standing):
                self.cli("approve", str(self.folder), "--note", "\n" + ship.standing_note() + " ")
                self.assertEqual(read_json(self.item.file("approval.json"))["by"], "standing")

    def test_legacy_lesson_command_uses_routed_writer_when_available(self):
        args = ["lesson", "add", "--title", "Check supported input", "--rule", "Reproduce with supported input.",
                "--scope", "all", "--pr", URL, "--enforced-by", "review"]
        with patch.object(lessons, "add_legacy", create=True, return_value="L-100") as routed, \
                patch.object(ledger, "add_lesson") as legacy:
            self.cli(*args)
            routed.assert_called_once_with("Check supported input", "Reproduce with supported input.", "all", URL, "review", "active", URL)
            legacy.assert_not_called()

    def test_lock_passes_generation_and_keeps_old_action_signature(self):
        with patch.object(shiftlock, "run", return_value={"ok": True, "generation": "lease"}) as run:
            self.assertEqual(self.cli("lock", "renew", "--owner", "shift", "--generation", "lease", "--ttl-hours", "8"), 0)
            run.assert_called_once_with("renew", "shift", 8.0, False, generation="lease")
        for action in ("acquire", "release", "status"):
            with self.subTest(action=action), patch.object(shiftlock, "run", autospec=lambda action, owner, ttl_hours, force: None,
                                                         return_value={"ok": True}) as run:
                self.assertEqual(self.cli("lock", action, "--owner", "shift"), 0)
                run.assert_called_once_with(action, "shift", 4, False)

    def test_dry_run_help_discloses_allowed_read_calls(self):
        for command in ("submit", "ship"):
            output = io.StringIO()
            with self.subTest(command=command), contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                cli.main([command, "--help"])
            self.assertEqual(raised.exception.code, 0)
            text = " ".join(output.getvalue().split())
            self.assertIn("no push", text)
            self.assertIn("read-only GitHub calls and fetch", text)
            for name in ("gate.json", "slop.json", "policy.json", "qualify.json"):
                self.assertIn(name, text)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            cli.main(["open-issue", "--help"])
        self.assertIn("text approval", " ".join(output.getvalue().split()))


class AtomicJson(unittest.TestCase):
    def test_new_files_respect_the_process_umask(self):
        with tempfile.TemporaryDirectory() as folder:
            for mask in (0o022, 0o002, 0o077):
                path = pathlib.Path(folder) / f"state-{mask}.json"
                previous = os.umask(mask)
                try:
                    write_json(path, {"value": "new"})
                finally:
                    os.umask(previous)
                self.assertEqual(path.stat().st_mode & 0o777, 0o666 & ~mask)


    def test_replace_uses_same_directory_and_retains_old_json_on_failure(self):
        from factory import util

        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "state.json"
            write_json(path, {"value": "before"})
            path.chmod(0o640)
            before = path.read_bytes()
            replace = os.replace

            def atomic(source, destination):
                self.assertEqual(pathlib.Path(source).parent, path.parent)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(read_json(source), {"value": "after"})
                replace(source, destination)

            with patch.object(util.os, "replace", side_effect=atomic) as called:
                write_json(path, {"value": "after"})
            called.assert_called_once()
            self.assertEqual(read_json(path), {"value": "after"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            with patch.object(util.os, "replace", side_effect=OSError("replacement failed")):
                with self.assertRaisesRegex(OSError, "replacement failed"):
                    write_json(path, {"value": "never visible"})
            self.assertEqual(read_json(path), {"value": "after"})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_schema_defaults_to_one_and_symlink_writes_keep_the_link(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = pathlib.Path(folder)
            target = directory / "target.json"
            write_json(target, {})
            (directory / "item.json").symlink_to(target.name)
            item = Item(directory)
            self.assertEqual(item.schema, 1)
            item.meta["schema"] = 2
            item.save()
            self.assertEqual(item.schema, 2)
            self.assertTrue(item.meta_path.is_symlink())
            self.assertEqual(read_json(target), {"schema": 2})


if __name__ == "__main__":
    unittest.main()
