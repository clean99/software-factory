import argparse
import copy
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from support import setup_module
from factory.core import checks, gate, review, verdicts
from factory.util import Item


HEAD = "a" * 40
OLD = "b" * 40
RECOMMIT = "c" * 40


def setUpModule():
    setup_module(unittest)


class ReviewProtocol(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.write("item.json", {"schema": 2, "repo": "example/repo", "clone": str(self.root / "clone"),
                                 "branch": "contrib/fix", "implementer": "builder",
                                 "test_cmd": "python -m unittest tests.test_parser"})
        self.item = Item(self.root)
        self.item.file("pr.md").write_text("Fix supported parser input.\n\nFixes #1\n")
        self.item.file("capture.txt").write_text("A user's supported input failed in a live capture.\n")
        self.item.file("contract.txt").write_text("The parser accepts the supported input.\n")
        self.demand = {
            "author": "scout", "claim": "Supported input fails parsing.",
            "who_hits_it": {"kind": "external-report", "source": "https://github.com/example/repo/issues/1",
                            "quote": "My supported input fails parsing.", "self_filed": False},
            "contract": {"says": "The parser accepts this input.", "source": "contract.txt"},
            "workaround": "No supported workaround.", "error_path": False, "real_occurrence": "",
            "decision": "build",
        }
        self.write("demand.json", self.demand)
        self.value = {"reviewer": "value-reader", "demand_sha256": hashlib.sha256(self.item.file("demand.json").read_bytes()).hexdigest(), "answers": {
            key: {"ok": True, "note": "Checked the external report and supported contract."}
            for key in review.VALUE_ANSWERS}, "verdict": "build"}
        self.review = {"reviewer": "independent-reader", "head": HEAD, "pr_sha256": verdicts.pr_sha256(self.item),
                       "obligations": [{"class": row["id"], "applies": False, "why": "The changed parser does not reach this path.", "receipts": []}
                                       for row in review.classes()], "findings": [], "verdict": "ship"}
        self.coordinator = {"head": HEAD, "pr_sha256": verdicts.pr_sha256(self.item),
                            "read": dict.fromkeys(("diff", "tests", "pr_text", "demand"), True),
                            "reruns": ["test-rerun"],
                            "counterexample": {"what": "Check a truncated token at the input boundary.", "receipt": "counter"},
                            "verdict": "ship"}
        self.store = {}
        self.receipt("test", ["python", "-m", "unittest", "tests.test_parser"])
        self.receipt("test-rerun", self.store["test"]["argv"], rerun_of="test", match={"exit": True, "output": True})
        self.receipt("counter", ["python", "probe-truncated.py"])
        self.item.meta["required_checks"] = [{"label": "regression", "argv": self.store["test"]["argv"], "cwd": "."}]
        self.write("redgreen.json", {"cmd": self.item.meta["test_cmd"], "cwd": ".", "head_sha": HEAD, "ok": True})
        self.write("item.json", self.item.meta)
        self.addCleanup(patch.stopall)
        patch("factory.core.clones.ensure").start()
        patch.object(Item, "head_sha", return_value=HEAD).start()
        self.bound = patch.object(verdicts, "bound", side_effect=lambda item, head, sha: head.startswith(sha) or (head, sha) == (HEAD, RECOMMIT)).start()
        patch.object(review.receipts, "load", side_effect=lambda item: list(self.store.values())).start()
        patch.object(review.receipts, "get", side_effect=lambda item, rid: self.store.get(rid)).start()
        patch.object(review.receipts, "valid", side_effect=lambda row: row.get("valid") is True).start()
        self.save()

    def write(self, name, data):
        (self.root / name).write_text(json.dumps(data))

    def save(self):
        for part in review.PARTS:
            self.write(review.PARTS[part], getattr(self, part))

    def receipt(self, rid, argv=None, **fields):
        self.store[rid] = {"id": rid, "argv": argv or ["python", rid + ".py"], "cwd": str(self.root / "clone"),
                           "cwd_head": HEAD, "item_head": HEAD, "head_before": HEAD, "head_after": HEAD,
                           "tracked_dirty_before": False, "tracked_dirty_after": False, "clean": True,
                           "exit": 0, "valid": True, **fields}
        return self.store[rid]

    def obligation(self, class_id):
        return next(row for row in self.review["obligations"] if row["class"] == class_id)

    def apply(self, class_id):
        row = self.obligation(class_id)
        row.update(applies=True, why="The diff reaches this risk; the probe exercises it.", receipts=[class_id])
        self.receipt(class_id)
        return row

    def problems(self, part):
        self.save()
        return review.VALIDATORS[part](self.item)

    def assertProblem(self, part, text):
        problems = self.problems(part)
        self.assertTrue(any(text in problem for problem in problems), problems)

    def test_complete_protocol_passes(self):
        for part in review.PARTS:
            with self.subTest(part=part):
                self.assertEqual(self.problems(part), [])

    def test_each_missing_or_malformed_file_reports_how_to_add_it(self):
        for part, name in review.PARTS.items():
            for content in (None, "{", "[]", "null", '"text"'):
                with self.subTest(part=part, content=content):
                    self.save()
                    if content is None:
                        self.item.file(name).unlink()
                    else:
                        self.item.file(name).write_text(content)
                    self.assertIn(f"Add a valid JSON object in {name}.", review.VALIDATORS[part](self.item))

    def test_each_demand_kind_passes_with_a_supported_source(self):
        for kind in review.DEMAND_KINDS:
            self.demand["who_hits_it"]["kind"] = kind
            self.assertEqual(self.problems("demand"), [])

    def test_sources_accept_urls_and_existing_item_relative_files(self):
        for source in ("https://example.org/report#comment", "http://example.org/report", "capture.txt", "./capture.txt"):
            with self.subTest(source=source):
                self.demand["who_hits_it"]["source"] = source
                self.assertEqual(self.problems("demand"), [])

    def test_sources_reject_missing_files_directories_absolute_paths_and_escapes(self):
        outside = self.root.parent / (self.root.name + "-outside.txt")
        outside.write_text("outside")
        self.addCleanup(outside.unlink)
        self.item.file("escape.txt").symlink_to(outside)
        for source in ("", None, 1, "missing.txt", ".", str(self.root / "capture.txt"),
                       "../" + outside.name, "escape.txt", "file:///etc/passwd", "https://", "https://exa mple.org/report"):
            with self.subTest(source=source):
                self.demand["who_hits_it"]["source"] = source
                self.assertProblem("demand", "who_hits_it.source")

    def test_demand_requires_claim_workaround_quote_and_boolean_flags(self):
        for key in ("claim", "workaround"):
            original = self.demand.pop(key)
            self.assertProblem("demand", key)
            self.demand[key] = original
        self.demand["who_hits_it"]["quote"] = " "
        self.assertProblem("demand", "who_hits_it.quote")
        self.demand["who_hits_it"]["self_filed"] = "false"
        self.assertProblem("demand", "self_filed as true or false")
        self.demand["error_path"] = 0
        self.assertProblem("demand", "error_path as true or false")
        self.demand["who_hits_it"]["kind"] = "probe"
        self.assertProblem("demand", "who_hits_it.kind")

    def test_documented_contract_requires_quoted_contract_and_source(self):
        self.demand["who_hits_it"]["kind"] = "documented-contract"
        for key in ("says", "source"):
            original = self.demand["contract"].pop(key)
            self.assertProblem("demand", "contract." + key)
            self.demand["contract"][key] = original
        self.demand["contract"] = []
        self.assertProblem("demand", "Add demand.contract with")

    def test_error_path_needs_live_occurrence_not_anything_llm_provider_docs(self):
        self.demand.update(error_path=True, real_occurrence="")
        self.demand["who_hits_it"].update(kind="documented-contract", source="https://example.org/provider/events")
        self.assertProblem("demand", "real_occurrence")
        for source in ("https://example.org/provider/events", "contract.txt", "missing-capture.txt"):
            self.demand["real_occurrence"] = source
            self.assertProblem("demand", "real_occurrence")
        for source in ("capture.txt", "https://github.com/example/repo/issues/7"):
            self.demand["real_occurrence"] = source
            self.assertEqual(self.problems("demand"), [])

    def test_postcss_self_filed_report_needs_second_external_source(self):
        self.demand["who_hits_it"].update(self_filed=True, source="capture.txt")
        self.demand["contract"] = {"says": "", "source": ""}
        self.assertProblem("demand", "second, external source")
        self.demand["real_occurrence"] = "./capture.txt"
        self.assertProblem("demand", "second, external source")
        self.demand["real_occurrence"] = "https://github.com/example/repo/issues/2"
        self.assertEqual(self.problems("demand"), [])
        self.demand["real_occurrence"] = ""
        self.demand["contract"] = {"says": "Supported input.", "source": "contract.txt"}
        self.assertProblem("demand", "second, external source")

    def test_external_report_can_be_the_source_and_real_occurrence(self):
        self.demand.update(error_path=True, real_occurrence=self.demand["who_hits_it"]["source"] + "#issuecomment-2")
        self.assertEqual(self.problems("demand"), [])
        self.demand["who_hits_it"]["self_filed"] = True
        self.assertProblem("demand", "second, external source")
        self.demand["who_hits_it"].update(kind="documented-contract", self_filed=False)
        self.assertProblem("demand", "real_occurrence")

    def test_query_identity_preserves_resource_parameters_and_sorts_them(self):
        base = "https://tracker.example.org/show_bug.cgi"
        self.demand["who_hits_it"].update(self_filed=True, source=base + "?id=1")
        self.demand["real_occurrence"] = base + "?id=2"
        self.assertEqual(self.problems("demand"), [])
        for suffix in ("?id=1&utm_source=mail", "?ref=feed&id=1#comment", "?source=share&id=1",
                       "?id=1&ref_src=mail&share=1&notification_referrer_id=2&view=1"):
            self.demand["real_occurrence"] = base + suffix
            self.assertProblem("demand", "second, external source")
        self.assertEqual(review._source(self.item, base + "?z=2&id=1&z=1"),
                         review._source(self.item, base + "?z=1&z=2&id=1#comment"))
        self.assertNotEqual(review._source(self.item, base + "?q=1"), review._source(self.item, base + "?q=2"))
        self.assertNotEqual(review._source(self.item, base + "?q="), review._source(self.item, base))

    def test_github_number_spaces_ignore_query_and_fragment_aliases(self):
        for kind in ("issues", "pull", "discussions"):
            source = f"https://github.com/example/repo/{kind}/7"
            self.demand["who_hits_it"].update(self_filed=True, source=source)
            self.demand["error_path"] = True
            for suffix in ("", "?q=1", "?q=2#issuecomment-5", "#issuecomment-5", "/files?view=1"):
                with self.subTest(kind=kind, suffix=suffix):
                    self.demand["real_occurrence"] = source + suffix
                    self.assertEqual(review._source(self.item, source), review._source(self.item, source + suffix))
                    self.assertProblem("demand", "second, external source")
        self.demand["who_hits_it"]["source"] = "https://github.com/example/repo/issues/7"
        self.demand["real_occurrence"] = "https://github.com/example/repo/pull/7?q=1"
        self.assertProblem("demand", "second, external source")
        for source in ("https://github.com/example/repo/discussions/7",
                       "https://github.com/example/repo/issues/8",
                       "https://github.com/example/other/issues/7",
                       "https://tracker.example.org/example/repo/issues/7"):
            with self.subTest(source=source):
                self.demand["real_occurrence"] = source
                self.assertEqual(self.problems("demand"), [])
        self.demand["who_hits_it"]["source"] = "https://github.com/example/repo/discussions/7"
        self.demand["real_occurrence"] = "https://github.com/example/repo/pull/7"
        self.assertEqual(self.problems("demand"), [])

    def test_optional_occurrence_must_be_a_valid_source_when_supplied(self):
        self.demand["real_occurrence"] = "missing.txt"
        self.assertProblem("demand", "real_occurrence")

    def test_all_demand_decisions_and_invalid_decision(self):
        for decision in ("build", "propose", "drop"):
            self.demand["decision"] = decision
            self.assertEqual(self.problems("demand"), [])
        self.demand["decision"] = "ship"
        self.assertProblem("demand", "demand.decision")

    def test_value_reviewer_must_differ_from_both_authors(self):
        for reviewer in ("builder", " BUILDER ", "scout", " SCOUT ", ""):
            self.value["reviewer"] = reviewer
            self.assertProblem("value", "reviewer")
        self.value["reviewer"] = "different-reader"
        del self.demand["author"]
        self.save()
        self.value["demand_sha256"] = hashlib.sha256(self.item.file("demand.json").read_bytes()).hexdigest()
        self.assertEqual(self.problems("value"), [])

    def test_value_review_binds_demand_bytes_not_later_pr_text(self):
        self.value.pop("demand_sha256")
        self.assertProblem("value", "rerun the value review")
        self.value["demand_sha256"] = hashlib.sha256(self.item.file("demand.json").read_bytes()).hexdigest()
        self.assertEqual(self.problems("value"), [])
        self.item.file("pr.md").write_text("Final implementation and test description.\n")
        self.assertEqual(self.problems("value"), [])
        self.item.file("pr.md").unlink()
        self.assertEqual(self.problems("value"), [])
        self.item.file("demand.json").write_text(json.dumps(self.demand) + "\n")
        self.assertIn("rerun the value review", " ".join(review.validate_value(self.item)))
        self.demand["claim"] = "A different demand."
        self.assertProblem("value", "stale")

    def test_every_value_answer_requires_boolean_and_note(self):
        for key in review.VALUE_ANSWERS:
            original = self.value["answers"][key]
            for answer in ({}, {"ok": 1, "note": "Checked."}, {"ok": "true", "note": "Checked."}, {"ok": True, "note": " "}):
                self.value["answers"][key] = answer
                self.assertProblem("value", key)
            self.value["answers"][key] = original
        self.value["answers"] = []
        self.assertProblem("value", "source_is_real")

    def test_any_false_value_answer_forbids_build_but_allows_propose_or_drop(self):
        for key in review.VALUE_ANSWERS:
            self.value["answers"][key]["ok"] = False
            self.value["verdict"] = "build"
            self.assertProblem("value", key)
            for verdict in ("propose", "drop"):
                self.value["verdict"] = verdict
                self.assertEqual(self.problems("value"), [])
            self.value["answers"][key]["ok"] = True
        self.value["verdict"] = "ship"
        self.assertProblem("value", "value-review.verdict")

    def test_starlight_intended_directory_selection_cannot_be_built(self):
        self.value["answers"]["not_by_design"] = {"ok": False, "note": "Explicit directory selection excludes sibling files; supported alternatives exist."}
        self.assertProblem("value", "not_by_design")

    def test_risk_classes_have_unique_ids_questions_probe_instructions_and_lessons(self):
        classes = review.classes()
        expected = {"shared-state-writers": ["L-072"], "mode-consumers": ["L-074"], "siblings": ["L-015"],
                    "server-accepts": ["L-041"], "streamed-input": ["L-058"], "contract-owner": ["L-048"],
                    "ci-parity": [], "pr-text": []}
        self.assertEqual({row["id"]: row["lessons"] for row in classes}, expected)
        self.assertEqual(len(classes), len(expected))
        for row in classes:
            self.assertTrue(row["question"].startswith("Does "))
            self.assertEqual(row["question"].count("?"), 1)
            self.assertTrue(row["how_to_probe"])

    def test_every_risk_class_is_required_once_with_reason_and_boolean(self):
        for row in list(self.review["obligations"]):
            self.review["obligations"].remove(row)
            self.assertProblem("review", row["class"])
            self.review["obligations"].append(row)
            original = copy.deepcopy(row)
            row["why"] = " "
            self.assertProblem("review", row["class"])
            row.update(original, applies="false")
            self.assertProblem("review", ".applies")
            row.update(original, receipts={})
            self.assertProblem("review", ".receipts")
            row.update(original)
        self.review["obligations"].append(copy.deepcopy(self.review["obligations"][0]))
        self.assertProblem("review", "exactly one")
        self.review["obligations"][-1]["class"] = "not-a-class"
        self.assertProblem("review", "class id")

    def test_rollup_reused_export_mode_requires_consumer_receipt(self):
        row = self.obligation("mode-consumers")
        row.update(applies=True, why="A module.exports value now reuses the default export mode.")
        self.assertProblem("review", "receipt for the applying mode-consumers")
        self.apply("mode-consumers")
        self.assertEqual(self.problems("review"), [])

    def test_rollup_late_shared_state_writer_requires_receipt(self):
        row = self.obligation("shared-state-writers")
        row.update(applies=True, why="Namespace decisions now read a set updated during linking.")
        self.assertProblem("review", "receipt for the applying shared-state-writers")
        self.apply("shared-state-writers")
        self.assertEqual(self.problems("review"), [])

    def test_each_applying_class_needs_existing_valid_receipt_at_head(self):
        for class_id in (row["id"] for row in review.classes()):
            row = self.apply(class_id)
            self.assertEqual(self.problems("review"), [])
            saved = self.store.pop(class_id)
            self.assertProblem("review", "valid receipt for " + class_id)
            self.store[class_id] = saved
            saved["valid"] = False
            self.assertProblem("review", "valid receipt for " + class_id)
            saved.update(valid=True, cwd_head=OLD)
            self.assertProblem("review", "cwd_head")
            saved.update(cwd_head=RECOMMIT, head_before=RECOMMIT, head_after=RECOMMIT)
            self.assertEqual(self.problems("review"), [])
            row["receipts"] = [None]
            self.assertProblem("review", "valid receipt for " + class_id)
            row["receipts"] = [class_id]

    def test_review_and_coordinator_bind_current_head_and_pr_text(self):
        for part in ("review", "coordinator"):
            data = getattr(self, part)
            for sha in (None, "", "a", OLD):
                data["head"] = sha
                self.assertProblem(part, ".head bound")
            data["head"] = HEAD[:7]
            self.assertEqual(self.problems(part), [])
            data["head"] = HEAD
            data["pr_sha256"] = "wrong"
            self.assertProblem(part, ".pr_sha256")
            data["pr_sha256"] = verdicts.pr_sha256(self.item)
        self.item.file("pr.md").write_text("Changed public claims.\n")
        for part in ("review", "coordinator"):
            self.assertProblem(part, ".pr_sha256")
        self.item.file("pr.md").unlink()
        self.assertProblem("review", ".pr_sha256")

    def test_recommitted_review_remains_bound(self):
        self.review["head"] = RECOMMIT
        self.assertEqual(self.problems("review"), [])
        self.bound.assert_any_call(self.item, HEAD, RECOMMIT)

    def finding(self, severity="blocking", **fields):
        self.review["findings"] = [{"severity": severity, "where": "src/parser.py:17",
                                    "summary": "A truncated token crashes the parser.", "receipt": "counter", **fields}]

    def test_blocking_finding_requires_receipt_and_forbids_ship_even_when_accepted(self):
        self.finding(accepted="Accept the failure.")
        self.assertProblem("review", "blocking finding remains")
        self.review["verdict"] = "fix-needed"
        self.assertEqual(self.problems("review"), [])
        self.review["findings"][0]["receipt"] = "missing"
        self.assertProblem("review", "valid receipt")
        del self.review["findings"][0]["receipt"]
        self.assertProblem("review", "valid receipt")

    def test_should_fix_needs_acceptance_reason_to_ship(self):
        self.finding("should-fix")
        for reason in (None, "", " ", True):
            self.review["findings"][0]["accepted"] = reason
            self.assertProblem("review", ".accepted")
        self.review["findings"][0]["accepted"] = "Maintainer requested this compatibility trade-off."
        self.assertEqual(self.problems("review"), [])
        self.review["findings"][0].pop("accepted")
        self.review["verdict"] = "fix-needed"
        self.assertEqual(self.problems("review"), [])

    def test_nit_and_preexisting_findings_allow_ship_without_receipt(self):
        for severity in ("nit", "pre-existing"):
            self.finding(severity, receipt="")
            self.assertEqual(self.problems("review"), [])

    def test_nonapplying_obligations_and_findings_can_cite_base_evidence(self):
        self.store["counter"].update(cwd_head=OLD, head_before=OLD, head_after=OLD)
        self.obligation("contract-owner")["receipts"] = ["counter"]
        self.finding("pre-existing")
        self.assertEqual(self.problems("review"), [])

    def test_findings_need_location_summary_and_known_severity(self):
        self.finding("invalid", where="", summary="")
        for field in ("where", "summary", "severity"):
            self.assertProblem("review", field)
        self.review["findings"] = None
        self.assertProblem("review", "findings as a list")
        self.review["reviewer"] = ""
        self.assertProblem("review", "review.reviewer")
        self.review["verdict"] = "build"
        self.assertProblem("review", "review.verdict")

    def test_coordinator_must_read_all_inputs(self):
        for key in self.coordinator["read"]:
            for value in (False, "true", 1, None):
                self.coordinator["read"][key] = value
                self.assertProblem("coordinator", "read." + key)
            self.coordinator["read"][key] = True
        self.assertEqual(self.problems("coordinator"), [])

    def test_coordinator_reruns_cover_each_applying_class_and_redgreen_command(self):
        for class_id in (row["id"] for row in review.classes()):
            self.apply(class_id)
            self.assertProblem("coordinator", "applying " + class_id)
            self.receipt(class_id + "-rerun", self.store[class_id]["argv"], rerun_of=class_id, match={"exit": True})
            self.coordinator["reruns"].append(class_id + "-rerun")
            self.assertEqual(self.problems("coordinator"), [])
        self.coordinator["reruns"].remove("test-rerun")
        self.assertProblem("coordinator", "redgreen.json cmd")

    def test_one_original_can_cover_more_than_one_obligation(self):
        for class_id in ("mode-consumers", "shared-state-writers"):
            row = self.apply(class_id)
            row["receipts"] = ["test"]
        self.assertEqual(self.problems("coordinator"), [])

    def test_a_rerun_can_be_rerun_again(self):
        self.receipt("test-again", self.store["test"]["argv"], rerun_of="test-rerun", match={"exit": True})
        self.coordinator["reruns"] = ["test-again"]
        self.assertEqual(self.problems("coordinator"), [])

    def test_reruns_require_valid_original_and_matching_exit_at_reviewed_head(self):
        rerun = self.store["test-rerun"]
        for fields, text in (({"valid": False}, "valid receipt"), ({"cwd_head": OLD}, "cwd_head"),
                             ({"rerun_of": ""}, "with rerun_of"), ({"rerun_of": "missing"}, "rerun original"),
                             ({"match": {}}, "match.exit true"), ({"match": {"exit": 1}}, "match.exit true")):
            original = copy.deepcopy(rerun)
            rerun.update(fields)
            self.assertProblem("coordinator", text)
            rerun.clear()
            rerun.update(original)
        self.store["test"]["valid"] = False
        self.assertProblem("coordinator", "rerun original")

    def test_mismatching_exit_forces_fix_needed_instead_of_ship_or_drop(self):
        self.store["test-rerun"]["match"]["exit"] = False
        for verdict in ("ship", "drop"):
            self.coordinator["verdict"] = verdict
            self.assertProblem("coordinator", "verdict: fix-needed")
        self.coordinator["verdict"] = "fix-needed"
        problems = self.problems("coordinator")
        self.assertTrue(any("match.exit true" in problem for problem in problems))
        self.assertFalse(any("verdict: fix-needed" in problem for problem in problems))

    def test_invalid_receipt_identifier_is_an_actionable_problem(self):
        self.apply("mode-consumers")["receipts"] = ["../outside"]
        with patch.object(review.receipts, "get", side_effect=ValueError("invalid receipt id")):
            self.assertProblem("review", "valid receipt")

    def test_unreadable_receipt_store_does_not_crash_the_coordinator_check(self):
        with patch.object(review.receipts, "load", side_effect=ValueError("invalid receipt id")):
            self.assertProblem("coordinator", "red-to-green test command")

    def test_output_mismatch_alone_does_not_fail_exit_match_rule(self):
        self.store["test-rerun"]["match"]["output"] = False
        self.assertEqual(self.problems("coordinator"), [])

    def test_test_command_accepts_direct_argv_and_explicit_shell(self):
        for argv in (["python", "-m", "unittest", "tests.test_parser"],
                     ["sh", "-c", self.item.meta["test_cmd"]], ["/bin/bash", "-lc", self.item.meta["test_cmd"]]):
            self.store["test"]["argv"] = argv
            self.store["test-rerun"]["argv"] = argv
            self.item.meta["required_checks"][0]["argv"] = argv
            self.assertEqual(self.problems("coordinator"), [])
        self.store["test"]["argv"] = ["python", "unrelated-test.py"]
        self.assertProblem("coordinator", "red-to-green test command")

    def test_redgreen_command_must_run_in_the_declared_test_directory(self):
        self.item.meta["test_cwd"] = "packages/parser"
        self.item.meta["required_checks"][0]["cwd"] = "packages/parser"
        self.write("redgreen.json", {"cmd": self.item.meta["test_cmd"], "cwd": "packages/parser", "head_sha": HEAD, "ok": True})
        self.assertProblem("coordinator", "red-to-green test command")
        for rid in ("test", "test-rerun"):
            self.store[rid]["cwd"] = str(self.root / "clone/packages/parser")
        self.assertEqual(self.problems("coordinator"), [])
        self.item.meta.pop("test_cmd")
        self.item.meta.pop("test_cwd")
        self.write("redgreen.json", {"cmd": "python -m unittest tests.test_parser", "cwd": "packages/other", "head_sha": HEAD, "ok": True})
        self.assertProblem("coordinator", "redgreen.json cmd")
        self.write("redgreen.json", {"cmd": "python -m unittest tests.test_parser", "cwd": "packages/parser", "head_sha": HEAD, "ok": True})
        self.assertEqual(self.problems("coordinator"), [])

    def test_negative_probe_receipts_can_support_findings(self):
        self.finding()
        self.review["verdict"] = "fix-needed"
        self.store["counter"].update(exit=1, expect_fail=True)
        self.assertEqual(self.problems("review"), [])

    def test_nested_malformed_review_data_is_reported_not_raised(self):
        for part, field in (("demand", "who_hits_it"), ("value", "answers"), ("review", "obligations"),
                            ("review", "findings"), ("coordinator", "reruns"), ("coordinator", "counterexample")):
            data = getattr(self, part)
            original = data[field]
            for invalid in (None, False, "text", [None, {}]):
                with self.subTest(part=part, field=field, invalid=invalid):
                    data[field] = invalid
                    self.assertTrue(self.problems(part))
            data[field] = original

    def test_redgreen_fallback_names_its_command_source(self):
        self.item.meta.pop("test_cmd")
        self.write("redgreen.json", {"cmd": "python -m unittest tests.test_parser", "head_sha": HEAD, "ok": True})
        self.assertEqual(self.problems("coordinator"), [])
        self.assertIn("redgreen.json", review._test_receipts(self.item)[1])
        self.coordinator["reruns"] = []
        self.assertProblem("coordinator", "redgreen.json cmd")

    def test_redgreen_file_is_not_itself_a_rerun_receipt(self):
        self.write("redgreen.json", {"cmd": self.item.meta["test_cmd"], "head_sha": HEAD, "ok": True})
        self.coordinator["reruns"] = ["redgreen.json"]
        self.assertProblem("coordinator", "valid receipt for coordinator rerun")

    def test_coordinator_requires_own_counterexample_not_reviewer_command(self):
        self.coordinator.pop("counterexample")
        self.assertProblem("coordinator", "counterexample")
        self.coordinator["counterexample"] = {"what": "Boundary input", "receipt": "counter"}
        self.apply("mode-consumers")["receipts"] = ["counter"]
        self.assertProblem("coordinator", "argv differs")
        self.obligation("mode-consumers").update(applies=False, receipts=[])
        self.finding("nit")
        self.assertProblem("coordinator", "argv differs")
        self.review["findings"] = []
        self.assertEqual(self.problems("coordinator"), [])

    def test_counterexample_needs_valid_receipt_at_head_and_description(self):
        for fields, text in (({"valid": False}, "valid receipt"), ({"cwd_head": OLD}, "cwd_head"), ({"argv": []}, "non-empty argv")):
            original = copy.deepcopy(self.store["counter"])
            self.store["counter"].update(fields)
            self.assertProblem("coordinator", text)
            self.store["counter"] = original
        self.coordinator["counterexample"]["what"] = ""
        self.assertProblem("coordinator", "counterexample.what")
        self.coordinator["verdict"] = "build"
        self.assertProblem("coordinator", "coordinator-review.verdict")

    def test_coordinator_without_valid_review_cannot_pass(self):
        self.review["obligations"] = []
        self.assertProblem("coordinator", "review obligation")

    def test_gate_registration_modes_and_schema(self):
        expected = {"demand-evidence": ("prepare", "review", "release"),
                    "value-review": ("prepare", "review", "release"),
                    "review-obligations": ("review", "release"), "coordinator-proof": ("release",)}
        registered = checks.registry()
        ours = {key: registered[key] for key in expected}
        for key, modes in expected.items():
            self.assertEqual(ours[key]["modes"], modes)
            self.assertEqual(ours[key]["schema_min"], 2)
        with patch.object(checks, "_REGISTRY", ours):
            for mode, count in (("prepare", 2), ("review", 3), ("release", 4)):
                report = gate.Report()
                checks.run_extra(self.item, report, mode)
                self.assertEqual(len(report.rows), count)
                self.assertTrue(report.ok, report.text())
            self.item.meta.pop("schema")
            report = gate.Report()
            checks.run_extra(self.item, report, "release")
            self.assertEqual(report.rows, [])

    def test_gate_row_lists_only_first_three_problems_and_fix_command(self):
        for part, function in (("demand", review.demand_evidence), ("value", review.value_review),
                               ("review", review.review_obligations), ("coordinator", review.coordinator_proof)):
            errors = [f"Add missing field {i}." for i in range(5)]
            with patch.dict(review.VALIDATORS, {part: lambda item: errors[:]}):
                report = gate.Report()
                function(self.item, report)
            self.assertEqual(len(report.rows), 1)
            row = report.rows[0]
            self.assertFalse(row["ok"])
            self.assertEqual(row["detail"], " ".join(errors[:3]))
            self.assertEqual(row["fix"], f"bin/factory review check --item {self.root} {part}")

    def test_value_gate_requires_build_even_when_propose_is_valid(self):
        self.value["verdict"] = "propose"
        self.assertEqual(self.problems("value"), [])
        report = gate.Report()
        review.value_review(self.item, report)
        self.assertFalse(report.ok)
        self.assertIn("verdict of build", report.text())

    def test_rounds_archive_without_overwriting_and_survive_reload(self):
        self.review["verdict"] = "fix-needed"
        self.save()
        self.write("review-round1.json", {"verdict": "fix-needed"})
        self.item.file("review-round3-old.md").write_text("verdict: fix-needed\n")
        self.item.file("review-round9.json").mkdir()
        self.assertEqual(review.rounds(self.item), 2)
        target = review.record_round(self.item)
        self.assertEqual(target.name, "review-round4.json")
        self.assertEqual(json.loads(target.read_text()), self.review)
        self.assertFalse(self.item.file("review.json").exists())
        self.assertEqual(review.rounds(Item(self.root)), 3)
        with self.assertRaises(SystemExit):
            review.record_round(self.item)
        self.assertEqual(json.loads(self.item.file("review-round1.json").read_text()), {"verdict": "fix-needed"})

    def test_legacy_round_archives_markdown(self):
        self.item.meta.pop("schema")
        self.item.file("review.md").write_text("head: " + HEAD + "\nverdict: fix-needed\n")
        self.assertEqual(review.record_round(self.item).name, "review-round1.md")
        self.assertTrue(self.item.file("review.json").exists())

    def test_second_fix_needed_review_requires_takeover(self):
        self.review["verdict"] = "fix-needed"
        self.save()
        self.assertFalse(review.needs_takeover(self.item))
        review.record_round(self.item)
        self.assertFalse(review.needs_takeover(self.item))
        self.save()
        self.assertTrue(review.needs_takeover(Item(self.root)))
        for verdict in ("ship", "drop"):
            self.review["verdict"] = verdict
            self.save()
            self.assertFalse(review.needs_takeover(self.item))

    def starts(self, *ages):
        current = time.time()
        self.write(review.timeline.FILE_NAME, [{"event": "review-started", "head": HEAD, "at": dt.datetime.fromtimestamp(
            current - age, dt.timezone.utc).isoformat()} for age in ages])
        return current

    def test_takeover_after_default_or_custom_budget_without_result(self):
        self.item.file("review.json").unlink()
        for budget, age, expected in ((120, None, False), (120, 7199, False), (120, 7201, True),
                                      (5, 299, False), (5, 301, True), ("bad", 7201, True)):
            with self.subTest(budget=budget, age=age):
                self.item.meta["review_budget_minutes"] = budget
                self.starts(*([] if age is None else [age]))
                self.assertEqual(review.needs_takeover(self.item), expected)

    def test_unstarted_review_does_not_require_a_head(self):
        for name in ("review.json", "coordinator-review.json"):
            self.item.file(name).unlink()
        at = dt.datetime.now(dt.timezone.utc).isoformat()
        for events in ([], [{"event": "implement-started", "at": at}],
                       [{"event": "review-started", "at": at}],
                       [{"event": "review-started", "at": at, "head": ""}],
                       [{"event": "review-started", "at": at, "head": " "}],
                       [{"event": "review-started", "at": at, "head": 123}],
                       [{"event": "review-started", "at": at, "head": [HEAD]}]):
            self.write(review.timeline.FILE_NAME, events)
            with patch.object(Item, "head_sha", side_effect=KeyError("branch")) as head:
                self.assertFalse(review.needs_takeover(self.item))
                head.assert_not_called()
        self.starts(8000)
        with patch.object(Item, "head_sha", side_effect=KeyError("branch")):
            self.assertTrue(review.needs_takeover(self.item))

    def test_takeover_decision_records_current_head_once_for_both_schemas(self):
        for schema in (1, 2):
            with self.subTest(schema=schema):
                self.item.meta.update(schema=schema, review_budget_minutes=5)
                self.item.save()
                self.starts(600)
                decision = {"decision": "repair", "reason": "Fix the missed boundary", "findings": [], "remaining": []}
                self.write("takeover.json", decision)
                self.invoke("takeover", "--item", str(self.root), "--head", HEAD)
                saved = json.loads(self.item.file("takeover.json").read_text())
                self.assertEqual(saved, {**decision, "head": HEAD})
                event = review.timeline.events(self.item)[-1]
                self.assertEqual(event["event"], "takeover-decided")
                self.assertEqual(event["head"], HEAD)
                self.assertEqual(event["decision"], "repair")
                self.assertIsNone(review._review_start(self.item))
                at = time.time() - 600
                review.timeline.mark(self.item, "review-started", head=HEAD,
                                     at=dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat())
                count = len(review.timeline.events(self.item))
                self.invoke("takeover", "--item", str(self.root), "--head", HEAD)
                self.assertEqual(len(review.timeline.events(self.item)), count)
                self.assertAlmostEqual(review._review_start(self.item), at, places=4)

    def test_takeover_decision_rejects_invalid_evidence_without_writing(self):
        for decision in ({}, {"decision": " "}, {"decision": "ship", "reason": "done"},
                         {"decision": "repair", "reason": " "}, {"decision": "park", "reason": "blocked"}):
            self.write("takeover.json", decision)
            before = self.item.file("takeover.json").read_bytes()
            with self.subTest(decision=decision), self.assertRaises(SystemExit):
                self.invoke("takeover", "--item", str(self.root), "--head", HEAD)
            self.assertEqual(self.item.file("takeover.json").read_bytes(), before)
            self.assertEqual(review.timeline.events(self.item), [])
        self.write("takeover.json", {"decision": "park", "reason": "blocked", "reopen_when": "A supported fix is available"})
        before = self.item.file("takeover.json").read_bytes()
        for head in (OLD, HEAD[:7]):
            with self.assertRaisesRegex(SystemExit, "current head"):
                self.invoke("takeover", "--item", str(self.root), "--head", head)
            self.assertEqual(self.item.file("takeover.json").read_bytes(), before)
        self.invoke("takeover", "--item", str(self.root), "--head", HEAD)
        self.assertEqual(review.timeline.events(self.item)[-1]["decision"], "park")

    def test_only_an_on_time_current_review_ends_timeout(self):
        current = self.starts(8000)
        path = self.item.file("review.json")
        os.utime(path, (current - 1000, current - 1000))
        self.assertFalse(review.needs_takeover(self.item))
        os.utime(path, (current - 1, current - 1))
        self.assertTrue(review.needs_takeover(self.item))
        self.review["head"] = OLD
        self.save()
        self.assertTrue(review.needs_takeover(self.item))
        self.review["head"] = HEAD
        self.save()
        os.utime(path, (current - 9000, current - 9000))
        self.assertTrue(review.needs_takeover(self.item))
        path.write_text("{")
        self.assertTrue(review.needs_takeover(self.item))

    def test_resources_are_normalised_before_comparing_occurrences(self):
        self.demand["error_path"] = True
        self.demand["who_hits_it"]["self_filed"] = True
        for source in ("https://GITHUB.com/Example/Repo/issues/1/#comment", "https://github.com/example/repo/pull/1/files?view=1",
                       "https://github.com/example/repo/issues/1?q=1", "https://github.com/example/repo/issues/1?utm_source=mail"):
            with self.subTest(source=source):
                self.demand["real_occurrence"] = source
                self.assertProblem("demand", "real_occurrence")
                self.assertProblem("demand", "second, external source")
        self.demand["who_hits_it"].update(kind="external-report", self_filed=False)
        self.demand["contract"] = {"says": "Supported.", "source": "HTTPS://Docs.Example.org/events/"}
        self.demand["real_occurrence"] = "https://docs.example.org/events?view=1#failed"
        self.assertProblem("demand", "real_occurrence")
        self.demand["real_occurrence"] = "https://github.com/example/repo/issues/2"
        self.assertEqual(self.problems("demand"), [])

    def test_dirty_receipts_cannot_support_current_head_proofs(self):
        self.apply("mode-consumers")
        for rid, part in (("mode-consumers", "review"), ("test", "coordinator"),
                          ("test-rerun", "coordinator"), ("counter", "coordinator")):
            with self.subTest(receipt=rid):
                self.store[rid]["clean"] = False
                self.assertProblem(part, "commit or revert")
                self.store[rid]["clean"] = True
        self.store["counter"].pop("clean")
        self.assertProblem("coordinator", "commit or revert")
        self.obligation("contract-owner")["receipts"] = ["counter"]
        self.assertProblem("review", "commit or revert")

    def test_redgreen_is_the_record_even_when_item_metadata_changes(self):
        self.item.meta["test_cmd"] = "true"
        self.item.meta["required_checks"] = [{"label": "test", "argv": ["true"], "cwd": "."}]
        self.store["test"]["argv"] = self.store["test-rerun"]["argv"] = ["true"]
        self.assertProblem("coordinator", "rerun bin/factory redgreen first")
        self.assertProblem("coordinator", "redgreen.json cmd")

    def test_redgreen_home_paths_match_scrubbed_receipts(self):
        command = "python -m unittest " + str(self.root / "case.py")
        self.item.meta["test_cmd"] = command
        self.write("redgreen.json", {"cmd": command, "cwd": str(self.root / "clone"), "ok": True, "head_sha": HEAD})
        argv = ["python", "-m", "unittest", "~/case.py"]
        for rid in ("test", "test-rerun"):
            self.store[rid].update(argv=argv, cwd="~/clone")
        self.item.meta["required_checks"][0]["argv"] = ["python", "-m", "unittest", str(self.root / "case.py")]
        with patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertEqual(self.problems("coordinator"), [])

    def test_redgreen_must_be_successful_and_bound_to_item_head(self):
        for change in ({"head_sha": OLD}, {"ok": False}, {"head_sha": None}, {"cmd": ""}):
            with self.subTest(change=change):
                self.write("redgreen.json", {"cmd": self.item.meta["test_cmd"], "cwd": ".", "ok": True, "head_sha": HEAD, **change})
                self.assertProblem("coordinator", "bin/factory redgreen")
        self.item.file("redgreen.json").unlink()
        self.assertProblem("coordinator", "bin/factory redgreen")

    def test_rerun_original_must_be_cited_by_review_or_required_checks(self):
        self.receipt("uncited", ["python", "other.py"])
        self.receipt("uncited-rerun", ["python", "other.py"], rerun_of="uncited", match={"exit": True})
        self.coordinator["reruns"].append("uncited-rerun")
        self.assertProblem("coordinator", "review or required_checks")
        self.obligation("mode-consumers")["receipts"] = ["uncited"]
        self.assertEqual(self.problems("coordinator"), [])

    def test_rerun_pair_must_really_use_same_command_cwd_and_exit(self):
        for field, value in (("argv", ["true"]), ("cwd", str(self.root)), ("exit", 7)):
            with self.subTest(field=field):
                old = self.store["test-rerun"][field]
                self.store["test-rerun"][field] = value
                self.assertProblem("coordinator", "same argv, cwd and exit")
                self.store["test-rerun"][field] = old

    def test_incomplete_review_forbids_ship_and_requires_takeover(self):
        for part in ("review", "coordinator"):
            data = getattr(self, part)
            for invalid in ("false", 0, None):
                data["complete"] = invalid
                self.assertProblem(part, ".complete as true or false")
            data["complete"] = False
            self.assertProblem(part, "incomplete")
            self.assertTrue(review.needs_takeover(Item(self.root)))
            data["verdict"] = "fix-needed"
            self.assertEqual(self.problems(part), [])
            data.update(complete=True, verdict="ship")

    def test_public_problems_returns_actionable_errors_without_raising(self):
        for part in review.PARTS:
            self.item.file(review.PARTS[part]).write_text("{")
            self.assertTrue(review.problems(self.item, part))
            self.save()
            with patch.object(review, "read_json", side_effect=PermissionError("unreadable")):
                self.assertTrue(review.problems(self.item, part))
        self.assertTrue(review.problems(self.item, "invalid"))

    def test_legacy_markdown_read_errors_name_the_file_and_fix(self):
        self.item.meta["schema"] = 1
        for name, reader in (("verify.md", verdicts.read_self_review), ("review.md", verdicts.read_review),
                             ("coordinator-review.md", verdicts.read_coordinator)):
            self.item.file(name).write_bytes(b"\xff")
            with self.subTest(name=name), self.assertRaisesRegex(verdicts.EvidenceError, name + ".*restore readable Markdown"):
                reader(self.item)
            with patch.object(pathlib.Path, "read_text", side_effect=PermissionError("denied")), \
                    self.assertRaisesRegex(verdicts.EvidenceError, name):
                reader(self.item)

    def test_gate_legacy_unreadable_markdown_is_a_failing_row(self):
        self.item.meta.update(schema=1, line="oss", stacked_on="main")
        for name, check_name in (("verify.md", "self-review"), ("review.md", "independent review"),
                                 ("coordinator-review.md", "coordinator review")):
            for filename in ("verify.md", "review.md", "coordinator-review.md"):
                self.item.file(filename).write_text("reviewer: reader\nhead: " + HEAD + "\nverdict: ship\n")
            self.item.file(name).write_bytes(b"\xff")
            report = gate.Report()
            with patch("factory.util.global_hooks_drift", return_value=[]), \
                    patch.object(gate, "git", side_effect=lambda *args, **kwargs: "0" if "rev-list" in args else ""), \
                    patch.object(gate, "sh", return_value=subprocess.CompletedProcess([], 0)), \
                    patch.object(gate.slop, "check", return_value={"ok": True, "stats": {"files": 1}, "warns": [], "fails": []}):
                gate.core_checks(self.item, report, HEAD)
                gate.coordinator_check(self.item, report, HEAD)
            row = next(row for row in report.rows if row["check"] == check_name)
            self.assertFalse(row["ok"])
            self.assertIn(name, row["detail"])
            self.assertIn("repair", row["fix"])

    def test_plan_catches_legacy_unreadable_self_review(self):
        from factory.oss import plan

        self.item.meta["schema"] = 1
        self.item.clone.mkdir()
        self.item.file("review.md").write_text("reviewer: reader\nhead: " + HEAD + "\nverdict: ship\n")
        self.item.file("verify.md").write_bytes(b"\xff")
        bucket, entry = plan.classify(self.item)
        self.assertEqual(bucket, "errors")
        self.assertIn("verify.md", entry["error"])

    def test_repeated_briefs_cannot_reset_a_running_review_budget(self):
        self.item.meta["review_budget_minutes"] = 5
        self.item.save()
        self.item.file("review.json").unlink()
        self.starts(600, 60)
        self.assertTrue(review.needs_takeover(Item(self.root)))

    def test_finish_late_then_print_brief_keeps_takeover_required(self):
        from factory.core import briefs

        self.item.meta["review_budget_minutes"] = 5
        for schema in (1, 2):
            with self.subTest(schema=schema):
                self.item.meta["schema"] = schema
                self.item.save()
                name = "review.md" if schema == 1 else "review.json"
                if schema == 1:
                    self.item.file(name).write_text("head: " + HEAD + "\nverdict: ship\n")
                current = self.starts(600)
                os.utime(self.item.file(name), (current - 1, current - 1))
                self.assertTrue(review.needs_takeover(Item(self.root)))
                with patch.object(briefs, "compose", return_value="review brief"), redirect_stdout(io.StringIO()):
                    parser = argparse.ArgumentParser()
                    briefs.register(parser.add_subparsers(required=True))
                    args = parser.parse_args(["brief", "review", "--item", str(self.root)])
                    args.fn(args)
                self.assertTrue(review.needs_takeover(Item(self.root)))

    def test_headless_starts_never_start_a_cycle_or_require_takeover(self):
        self.item.meta["review_budget_minutes"] = 5
        current = 1800000000
        for schema in (1, 2):
            self.item.meta["schema"] = schema
            archive = self.item.file("review-round1.md" if schema == 1 else "review-round1.json")
            for archive_head in (None, OLD, HEAD):
                if archive_head is None:
                    archive.unlink(missing_ok=True)
                elif schema == 1:
                    archive.write_text("head: " + archive_head + "\nverdict: ship\n")
                else:
                    self.write(archive.name, {"head": archive_head, "verdict": "ship"})
                if archive.exists():
                    os.utime(archive, (current - 600, current - 600))
                for fields in ({}, {"head": None}, {"head": ""}):
                    with self.subTest(schema=schema, archive_head=archive_head, fields=fields):
                        events = [{"event": "review-started", "at": dt.datetime.fromtimestamp(
                            current - age, dt.timezone.utc).isoformat(), **fields} for age in (900, 600, 500)]
                        self.write(review.timeline.FILE_NAME, events)
                        with patch.object(review, "git", return_value=str(current - 600)) as git, \
                                patch.object(review.time, "time", return_value=current):
                            self.assertIsNone(review._review_start(self.item))
                            self.assertFalse(review.needs_takeover(self.item))
                        git.assert_not_called()

    def test_headless_start_cannot_reset_a_head_bound_cycle(self):
        self.item.meta["review_budget_minutes"] = 5
        current = self.cycle_events((1000, "review-started", {"head": HEAD}),
                                    (10, "review-started", {"failed_rounds": 99}))
        with patch.object(review, "git", return_value=str(int(current) - 2000)):
            self.assertAlmostEqual(review._review_start(self.item), current - 1000, places=4)
            self.assertTrue(review.needs_takeover(self.item))

    def test_headless_starts_skip_timestamps_before_parsing(self):
        from factory.core import nextaction

        for name in ("review.json", "coordinator-review.json"):
            self.item.file(name).unlink()
        for schema in (1, 2):
            self.item.meta.update(schema=schema, review_budget_minutes=5)
            self.starts(600)
            start = review.timeline.events(self.item)[0]
            recent = dt.datetime.now(dt.timezone.utc).isoformat()
            for fields in ({}, {"head": None}, {"head": ""}, {"head": " "},
                           {"head": 123}, {"head": []}, {"head": [HEAD]}):
                for at in (None, 123, [], "garbage", recent):
                    with self.subTest(schema=schema, fields=fields, at=at):
                        self.write(review.timeline.FILE_NAME, [start, {
                            "event": "review-started", "at": at, "failed_rounds": 99, **fields}])
                        with patch.object(review, "parse_iso", wraps=review.parse_iso) as parse:
                            self.assertTrue(review.needs_takeover(self.item))
                        parse.assert_called_once_with(start["at"])
                        actions, task = nextaction.item_actions(self.item, "unreviewed", {}, [])
                        self.assertEqual([action["kind"] for action in actions], ["takeover"])
                        self.assertIsNone(task)

    def test_malformed_event_timestamps_preserve_the_review_cycle(self):
        self.item.file("review.json").unlink()
        for schema in (1, 2):
            self.item.meta.update(schema=schema, review_budget_minutes=5)
            current = self.starts(600)
            start = review.timeline.events(self.item)[0]
            for event in ("review-started", "review-round-recorded", "takeover-decided", "implement-started"):
                for at in (None, 123, [], "garbage"):
                    with self.subTest(schema=schema, event=event, at=at):
                        self.write(review.timeline.FILE_NAME, [start, {
                            "event": event, "at": at, "head": HEAD, "failed_rounds": 99, "decision": "repair"}])
                        self.assertAlmostEqual(review._review_start(self.item), current - 600, places=4)
                        self.assertTrue(review.needs_takeover(self.item))

    def test_headless_start_then_brief_starts_the_current_cycle(self):
        from factory.core import briefs

        for name in ("review.json", "coordinator-review.json"):
            self.item.file(name).unlink()
        for schema in (1, 2):
            with self.subTest(schema=schema):
                self.item.meta.update(schema=schema, review_budget_minutes=5)
                self.item.save()
                current = self.cycle_events((1000, "review-started", {}))
                with patch.object(review, "git", return_value=str(int(current) - 2000)), \
                        patch.object(review.time, "time", return_value=current), \
                        patch.object(review.timeline, "now", return_value=dt.datetime.fromtimestamp(current, dt.timezone.utc)), \
                        patch.object(briefs, "compose", return_value="review brief"), redirect_stdout(io.StringIO()):
                    self.assertIsNone(review._review_start(self.item))
                    self.assertFalse(review.needs_takeover(self.item))
                    args = argparse.Namespace(role="review", item=str(self.root), repo=None, pr=None, task=None)
                    briefs.cmd_brief(args)
                    self.assertAlmostEqual(review._review_start(self.item), current, places=4)
                    self.assertFalse(review.needs_takeover(self.item))
                with patch.object(review.time, "time", return_value=current + 301):
                    self.assertTrue(review.needs_takeover(self.item))

    def test_head_bound_start_at_commit_second_counts_but_older_head_does_not(self):
        self.item.meta["review_budget_minutes"] = 5
        self.item.file("review.json").unlink()
        committed = 1800000000
        at = dt.datetime.fromtimestamp(committed, dt.timezone.utc).isoformat()
        for head, expected in ((HEAD, committed), (OLD, None)):
            with self.subTest(head=head):
                self.write(review.timeline.FILE_NAME, [{"event": "review-started", "head": head, "at": at}])
                with patch.object(review, "git", return_value=str(committed)) as git, \
                        patch.object(review.time, "time", return_value=committed + 1000):
                    self.assertEqual(review._review_start(self.item), expected)
                    self.assertEqual(review.needs_takeover(self.item), head == HEAD)
                git.assert_not_called()

    def test_new_legacy_head_and_brief_schedule_review_not_historical_takeover(self):
        from factory import cli
        from factory.core import nextaction
        from factory.oss import plan
        from factory.util import CONFIG

        self.item.meta.update(schema=1, review_budget_minutes=5)
        self.item.save()
        self.item.clone.mkdir()
        self.item.file("verify.md").write_text("verdict: ship\nWho hits it: parser users\nImpact: titles are retained\n")
        self.item.file("coordinator-review.md").write_text("head: " + OLD + "\nverdict: ship\n")
        current = time.time()
        archive = self.item.file("review-round1.md")
        archive.write_text("head: " + OLD + "\nverdict: ship\n")
        os.utime(archive, (current - 900, current - 900))
        self.write(review.timeline.FILE_NAME, [{"event": "review-started", "at": dt.datetime.fromtimestamp(
            current - 1000, dt.timezone.utc).isoformat()}])
        lessons = self.item.file("lessons.txt")
        lessons.write_text("")
        with patch.object(review, "git", return_value=str(int(current) - 100)), \
                patch.dict(CONFIG, lessons_file=str(lessons), runs_dir=str(self.root),
                           ledger_file=str(self.root / "ledger.json")), redirect_stdout(io.StringIO()) as output:
            cli.main(["brief", "review", "--item", str(self.root)])
            self.assertIn(HEAD, output.getvalue())
            item = Item(self.root)
            self.assertFalse(review.needs_takeover(item))
            self.assertEqual(review.rounds(item), 0)
            self.assertEqual(review.timeline.events(item)[-1]["head"], HEAD)
            bucket, entry = plan.classify(item)
            self.assertEqual(bucket, "unreviewed")
            actions, task = nextaction.item_actions(item, bucket, entry, [])
            self.assertEqual(actions, [])
            self.assertEqual(task["role"], "review")

    def cycle_events(self, *events):
        current = time.time()
        self.write(review.timeline.FILE_NAME, [{"at": dt.datetime.fromtimestamp(current - age, dt.timezone.utc).isoformat(),
                                      "event": event, **fields} for age, event, fields in events])
        return current

    def test_only_explicit_cycle_transitions_reset_the_first_start(self):
        self.item.meta["review_budget_minutes"] = 5
        for event, fields in (("review-round-recorded", {"head": HEAD, "failed_rounds": 1}),
                              ("takeover-decided", {"head": HEAD, "decision": "repair"})):
            with self.subTest(event=event):
                self.cycle_events((1000, "review-started", {"head": HEAD, "failed_rounds": 0}),
                                  (250, event, fields),
                                  (200, "review-started", {"head": HEAD}), (10, "review-started", {"head": HEAD}))
                self.assertFalse(review.needs_takeover(self.item))
        self.cycle_events((1000, "review-started", {"head": OLD, "failed_rounds": 0}),
                          (200, "review-started", {"head": HEAD, "failed_rounds": 0}),
                          (10, "review-started", {"head": HEAD, "failed_rounds": 0}))
        self.assertFalse(review.needs_takeover(self.item))
        self.cycle_events((1000, "review-started", {"head": HEAD, "failed_rounds": 0}),
                          (200, "review-started", {"head": HEAD, "failed_rounds": 1}),
                          (10, "review-started", {"head": HEAD, "failed_rounds": 1}))
        self.assertFalse(review.needs_takeover(self.item))

    def test_completion_or_unrelated_takeover_does_not_begin_a_cycle(self):
        self.item.meta["review_budget_minutes"] = 5
        for event, fields in (("review-finished", {"head": HEAD}),
                              ("takeover-started", {"head": HEAD}),
                              ("takeover-decided", {"head": OLD, "decision": "repair"})):
            with self.subTest(event=event):
                self.cycle_events((1000, "review-started", {"head": HEAD}), (200, event, fields),
                                  (10, "review-started", {"head": HEAD}))
                self.assertTrue(review.needs_takeover(self.item))

    def test_record_round_records_a_timeline_cycle_boundary(self):
        self.review["verdict"] = "fix-needed"
        self.save()
        review.record_round(self.item)
        row = review.timeline.events(self.item)[-1]
        self.assertEqual(row["event"], "review-round-recorded")
        self.assertEqual(row["head"], HEAD)
        self.assertEqual(row["failed_rounds"], 1)

    def test_successful_archives_end_fix_cycles_and_mirrors_do_not_double_count(self):
        for schema in (1, 2):
            with self.subTest(schema=schema):
                self.item.meta["schema"] = schema
                self.review["verdict"] = "fix-needed"
                self.save()
                self.item.file("review.md").write_text("head: " + HEAD + "\nverdict: fix-needed\n")
                self.write("review-round1.json", {"verdict": "fix-needed"})
                self.item.file("review-round1.md").write_text("verdict: fix-needed\n")
                self.write("review-round2.json", {"verdict": "ship"})
                self.item.file("review-round2.md").write_text("verdict: ship\n")
                self.assertEqual(review.rounds(self.item), 0)
                self.assertFalse(review.needs_takeover(self.item))
                self.write("review-round3.json", {"verdict": "fix-needed"})
                self.item.file("review-round3.md").write_text("verdict: fix-needed\n")
                self.assertEqual(review.rounds(self.item), 1)
                self.assertTrue(review.needs_takeover(self.item))
                self.item.file("review-round3.json").unlink()
                self.item.file("review-round3.md").unlink()

    def test_stamp_binds_current_head_and_normalised_pr_text(self):
        self.item.meta.update(issue_first="issue", issue=None)
        self.item.save()
        self.item.file("pr.md").write_text("Fix supported input.\n\nFixes #ISSUE\n")
        for part in ("review", "coordinator"):
            with self.subTest(part=part):
                self.invoke("stamp", "--item", str(self.root), part, "--head", HEAD)
                stamped = json.loads(self.item.file(review.PARTS[part]).read_text())
                self.assertEqual(stamped["head"], HEAD)
                self.assertEqual(stamped["pr_sha256"], verdicts.pr_text_sha256(self.item))
        self.item.meta["issue"] = 42
        self.item.save()
        self.item.file("pr.md").write_text("Fix supported input.\n\nFixes #42\n")
        for part in ("value", "review", "coordinator"):
            self.assertEqual(review.problems(self.item, part), [])
        self.item.file("pr.md").write_text("Different claim.\n\nFixes #42\n")
        self.assertEqual(review.problems(self.item, "value"), [])
        for part in ("review", "coordinator"):
            self.assertTrue(review.problems(self.item, part))

    def test_rebinding_a_finished_review_to_new_pr_text_keeps_its_finish_time(self):
        self.item.meta["review_budget_minutes"] = 5
        self.item.save()
        for schema in (1, 2):
            with self.subTest(schema=schema):
                self.item.meta["schema"] = schema
                self.item.save()
                name = "review.md" if schema == 1 else "review.json"
                if schema == 1:
                    self.item.file(name).write_text("head: " + HEAD + "\nverdict: ship\n")
                current = self.starts(600)
                finished = current - 400
                os.utime(self.item.file(name), (finished, finished))
                self.assertFalse(review.needs_takeover(Item(self.root)))
                self.item.file("pr.md").write_text("Coordinator rewrite of the claim.\n")
                self.invoke("stamp", "--item", str(self.root), "review", "--head", HEAD)
                self.assertAlmostEqual(self.item.file(name).stat().st_mtime, finished, places=3)
                self.assertFalse(review.needs_takeover(Item(self.root)))
                if schema == 2:
                    self.assertEqual(json.loads(self.item.file(name).read_text())["pr_sha256"],
                                     verdicts.pr_text_sha256(self.item))

    def test_first_stamp_of_an_unbound_or_unfinished_review_counts_as_its_finish(self):
        current = self.starts(600)
        early = current - 590
        for change in ({"head": None}, {"verdict": None}, {"complete": False, "verdict": None}):
            with self.subTest(change=change):
                data = json.loads(self.item.file("review.json").read_text())
                data.update(change)
                self.write("review.json", data)
                os.utime(self.item.file("review.json"), (early, early))
                self.invoke("stamp", "--item", str(self.root), "review", "--head", HEAD)
                self.assertGreater(self.item.file("review.json").stat().st_mtime, current - 5)

    def test_stamp_value_needs_no_head_or_pr_and_changes_only_binding_fields(self):
        self.item.meta.pop("clone")
        self.item.save()
        self.item.file("pr.md").unlink()
        original = {**self.value, "head": OLD, "pr_sha256": "old"}
        self.write("value-review.json", original)
        with patch.object(Item, "head_sha", side_effect=AssertionError("value has no code head")):
            self.invoke("stamp", "--item", str(self.root), "value")
        stamped = json.loads(self.item.file("value-review.json").read_text())
        self.assertEqual(stamped, self.value)
        self.assertEqual(verdicts.read_value_review(self.item)["demand_sha256"], self.value["demand_sha256"])

    def test_stamp_legacy_replaces_or_adds_head_without_altering_prose(self):
        self.item.meta["schema"] = 1
        self.item.save()
        for part, name, reader in (("review", "review.md", verdicts.read_review),
                                   ("coordinator", "coordinator-review.md", verdicts.read_coordinator)):
            for line in ("", "head: " + OLD + "\n", "  HEAD : invalid # old binding\n"):
                with self.subTest(part=part, line=line):
                    text = "# Review\n\nreviewer: reviewer\n" + line + "\nFindings stay here.\nverdict: ship\n"
                    self.item.file(name).write_text(text)
                    self.invoke("stamp", "--item", str(self.root), part, "--head", HEAD)
                    actual = self.item.file(name).read_text()
                    self.assertEqual(reader(self.item)["head"], HEAD)
                    expected = text.replace(line, "head: " + HEAD + "\n", 1) if line else "head: " + HEAD + "\n" + text
                    self.assertEqual(actual, expected)

    def test_stamp_never_creates_a_review_or_accepts_a_stale_head(self):
        for schema in (1, 2):
            self.item.meta["schema"] = schema
            self.item.save()
            for part in ("review", "coordinator", "value"):
                name = review.PARTS[part] if schema == 2 or part == "value" else review.PARTS[part].replace(".json", ".md")
                self.item.file(name).write_text("preserve these bytes")
                for head in (OLD, RECOMMIT, HEAD[:7]):
                    with self.subTest(schema=schema, part=part, head=head), self.assertRaises(SystemExit):
                        self.invoke("stamp", "--item", str(self.root), part, "--head", head)
                    self.assertEqual(self.item.file(name).read_text(), "preserve these bytes")
                self.item.file(name).unlink()
                with self.assertRaises(SystemExit):
                    self.invoke("stamp", "--item", str(self.root), part, "--head", HEAD)
                self.assertFalse(self.item.file(name).exists())
        self.item.meta["schema"] = 1
        self.item.save()
        with self.assertRaisesRegex(SystemExit, "legacy items have no value review"):
            self.invoke("stamp", "--item", str(self.root), "value")

    def test_composed_review_brief_stamp_and_check_commands_execute_for_both_schemas(self):
        from factory.core import briefs

        for schema in (1, 2):
            self.item.meta["schema"] = schema
            self.item.save()
            for role, part in (("review", "review"), ("coordinator-review", "coordinator")):
                with self.subTest(schema=schema, role=role):
                    name = review.PARTS[part]
                    if schema == 1:
                        name = name.replace(".json", ".md")
                        self.item.file(name).write_text("reviewer: reader\nverdict: ship\n")
                    with patch.object(briefs.lessons, "select", return_value=[]), patch.object(briefs, "_repo_card", return_value=""), \
                            patch.object(briefs, "_duties", return_value=""):
                        text = briefs.compose(role, self.item)
                    commands = re.findall(r"`(bin/factory review (?:stamp|check) [^`]+)`", text)
                    self.assertEqual(len(commands), 2, text)
                    before = self.item.file(name).read_bytes()
                    for command in commands:
                        argv = shlex.split(command)
                        self.invoke(*argv[2:])
                    result = verdicts.read_review(self.item) if part == "review" else verdicts.read_coordinator(self.item)
                    self.assertEqual(result["head"], HEAD)
                    if schema == 2:
                        old = json.loads(before)
                        new = json.loads(self.item.file(name).read_text())
                        self.assertEqual({k: v for k, v in new.items() if k not in ("head", "pr_sha256")},
                                         {k: v for k, v in old.items() if k not in ("head", "pr_sha256")})

    def test_stamp_refuses_stale_or_recommitted_head_without_writing(self):
        before = self.item.file("review.json").read_bytes()
        for sha in (OLD, RECOMMIT, HEAD[:7]):
            with self.subTest(sha=sha), self.assertRaisesRegex(SystemExit, "current head"):
                self.invoke("stamp", "--item", str(self.root), "review", "--head", sha)
            self.assertEqual(self.item.file("review.json").read_bytes(), before)

    def test_role_timeline_preserves_unrelated_issue_timeline(self):
        evidence = self.item.file("timeline.json")
        original = b'[\n  {"id": 1, "event": "commented", "body": "Issue evidence"}\n]\n'
        evidence.write_bytes(original)
        previous = review.timeline.events(self.item)
        event = review.timeline.mark(self.item, "review-started", head=HEAD)
        self.assertEqual(evidence.read_bytes(), original)
        self.assertEqual(previous, [])
        self.assertEqual(review.timeline.events(self.item), [event])
        self.assertEqual(json.loads(self.item.file("role-timeline.json").read_text()), [event])
        self.assertTrue(self.item.file(".role-timeline.lock").is_file())
        self.assertFalse(self.item.file(".timeline.lock").exists())

    def test_timeline_keeps_subsecond_order_for_review_boundaries(self):
        at = dt.datetime(2026, 1, 1, microsecond=123456, tzinfo=dt.timezone.utc)
        with patch.object(review.timeline, "now", return_value=at):
            event = review.timeline.mark(self.item, "review-started")
        self.assertEqual(dt.datetime.fromisoformat(event["at"]), at)

    def test_timeline_concurrent_writers_keep_both_events(self):
        first_read, second_read, release = threading.Event(), threading.Event(), threading.Event()
        original = review.timeline.events
        def paused(item):
            rows = original(item)
            if not first_read.is_set():
                first_read.set()
                release.wait(3)
            else:
                second_read.set()
            return rows
        with patch.object(review.timeline, "events", side_effect=paused), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(review.timeline.mark, self.item, "first")
            self.assertTrue(first_read.wait(3))
            second = pool.submit(review.timeline.mark, self.item, "second")
            second_read.wait(0.1)
            release.set()
            first.result(timeout=3)
            second.result(timeout=3)
        self.assertEqual({row["event"] for row in original(self.item)}, {"first", "second"})

    def invoke(self, *args):
        parser = argparse.ArgumentParser()
        review.register(parser.add_subparsers(required=True))
        parsed = parser.parse_args(["review", *args])
        output = io.StringIO()
        with redirect_stdout(output):
            parsed.fn(parsed)
        return output.getvalue()

    def test_cli_classes_and_check_each_part_or_all(self):
        self.assertEqual(json.loads(self.invoke("classes")), review.classes())
        for part in review.PARTS:
            self.invoke("check", "--item", str(self.root), part)
        self.assertIn("redgreen.json cmd", self.invoke("check", "--item", str(self.root)))
        self.value["answers"]["not_by_design"]["ok"] = False
        self.save()
        with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as error:
            review.cmd_check(argparse.Namespace(item=str(self.root), part="value"))
        self.assertEqual(error.exception.code, 1)
        self.assertIn("not_by_design", output.getvalue())

    def test_cli_round(self):
        self.assertIn("review-round1.json", self.invoke("round", "--item", str(self.root)))
        self.assertFalse(self.item.file("review.json").exists())

    def test_cli_legacy_check_does_not_demand_schema_two_files(self):
        meta = dict(self.item.meta)
        meta.pop("schema")
        self.write("item.json", meta)
        for name in review.PARTS.values():
            self.item.file(name).unlink()
        self.assertIn("Legacy item", self.invoke("check", "--item", str(self.root)))

    def test_render_is_one_way_and_writes_all_three_views(self):
        self.apply("mode-consumers")
        self.finding("should-fix", accepted="Accepted compatibility trade-off.")
        self.save()
        before = {name: self.item.file(name).read_bytes() for name in review.PARTS.values()}
        output = self.invoke("render", "--item", str(self.root))
        for name in ("review.md", "coordinator-review.md", "value-review.md"):
            self.assertIn(name, output)
            self.assertIn("Edit the JSON", self.item.file(name).read_text())
        self.assertIn("mode-consumers", self.item.file("review.md").read_text())
        self.assertIn("Accepted compatibility trade-off", self.item.file("review.md").read_text())
        self.assertIn("probe", self.item.file("review.md").read_text())
        self.assertIn("input boundary", self.item.file("coordinator-review.md").read_text())
        self.assertIn("Checked the external report", self.item.file("value-review.md").read_text())
        self.item.file("review.md").write_text("verdict: drop\n")
        self.assertEqual(verdicts.read_review(self.item)["verdict"], "ship")
        self.assertEqual(before, {name: self.item.file(name).read_bytes() for name in review.PARTS.values()})

    def test_render_refuses_legacy_or_absent_json(self):
        self.item.meta["schema"] = 1
        with self.assertRaisesRegex(SystemExit, "schema-2"):
            review.render(self.item)
        self.item.meta["schema"] = 2
        for name in review.PARTS.values():
            self.item.file(name).unlink()
        with self.assertRaisesRegex(SystemExit, "before rendering"):
            review.render(self.item)


if __name__ == "__main__":
    unittest.main()
