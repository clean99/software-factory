"""Parking is one command, and review independence trusts the transcripts over typed names."""
import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from support import setup_module
from factory import cli, util
from factory.core import gate, nextaction, provenance
from factory.util import Item, write_json


def setUpModule():
    setup_module(unittest)


class Workspace(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name).resolve()
        self.dir = self.root / "runs" / "2026-01-01" / "fix"
        self.dir.mkdir(parents=True)
        write_json(self.dir / "item.json", {"repo": "o/r", "branch": "contrib/fix", "base": "main", "status": "ready",
                                            "kind": "small", "implementer": "claude-code", "schema": 2})

    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            try:
                code = cli.main(list(args))
            except SystemExit as error:
                return error.code, output.getvalue()
        return code, output.getvalue()

    def meta(self):
        return json.loads((self.dir / "item.json").read_text())


class Park(Workspace):
    REASON = "The fork push is refused here; rerun ship when publication is available."

    def test_park_records_status_reason_and_time(self):
        code, text = self.call("park", str(self.dir), "--reason", "  " + self.REASON + "  ", "--reopen-when", "pushes work again")
        self.assertEqual(code, 0)
        meta = self.meta()
        self.assertEqual((meta["status"], meta["parked_reason"], meta["reopen_when"]), ("parked", self.REASON, "pushes work again"))
        self.assertIn("parked_at", meta)
        self.assertEqual(json.loads(text)["status"], "parked")

    def test_blocked_uses_the_blocked_reason_the_plan_reads(self):
        self.assertEqual(self.call("park", str(self.dir), "--reason", self.REASON, "--blocked")[0], 0)
        meta = self.meta()
        self.assertEqual((meta["status"], meta["blocked_reason"]), ("blocked", self.REASON))
        self.assertNotIn("parked_reason", meta)

    def test_a_reason_must_be_a_sentence_and_finished_items_stay_as_they_are(self):
        code, _ = self.call("park", str(self.dir), "--reason", "later")
        self.assertIn("reason", str(code))
        self.assertEqual(self.meta()["status"], "ready")
        for status in ("merged", "closed", "dropped", "abandoned"):
            with self.subTest(status=status):
                write_json(self.dir / "item.json", {**self.meta(), "status": status})
                code, _ = self.call("park", str(self.dir), "--reason", self.REASON)
                self.assertIn(status, str(code))
                self.assertEqual(self.meta()["status"], status)

    def test_actions_name_the_command(self):
        with patch.object(nextaction, "item_target", return_value="runs/2026-01-01/fix"):
            text = nextaction.parking(Item(self.dir))
        self.assertIn("bin/factory park runs/2026-01-01/fix --reason", text)
        self.assertNotIn("edit", text)


class TranscriptNames(Workspace):
    def names(self, reviewer):
        return gate.independence_names(Item(self.dir), reviewer)

    def test_without_provenance_the_typed_names_stand(self):
        self.assertEqual(self.names("other-model"), ("other-model", "claude-code"))

    def test_transcript_models_replace_typed_names(self):
        write_json(self.dir / "provenance.json", [
            {"role": "implement", "agent": "a1", "model": "cheap-model"},
            {"role": "review", "agent": "a0", "model": "old-model"},
            {"role": "review", "agent": "a2", "model": "cheap-model"},
        ])
        reviewer, implementer = self.names("some-reviewer")
        self.assertEqual(implementer, "cheap-model")
        self.assertTrue(reviewer.startswith("cheap-model (fresh context"))
        self.assertEqual(gate.review_independence(reviewer, implementer, "small")[0], "warn")
        self.assertEqual(gate.review_independence(reviewer, implementer, "large")[0], "fail")
        self.assertEqual(gate.review_independence("some-reviewer", "claude-code", "large")[0], "ok")

    def test_a_reviewer_that_names_its_real_model_is_left_alone(self):
        write_json(self.dir / "provenance.json", [{"role": "implement", "agent": "a1", "model": "cheap-model"},
                                                   {"role": "review", "agent": "a2", "model": "strong-model"}])
        self.assertEqual(self.names("strong-model (fresh context)"), ("strong-model (fresh context)", "cheap-model"))
        self.assertEqual(gate.review_independence("strong-model (fresh context)", "cheap-model", "large")[0], "ok")

    def test_unreadable_or_malformed_provenance_is_ignored(self):
        (self.dir / "provenance.json").write_text("{not json")
        self.assertEqual(provenance.models(Item(self.dir)), {})
        write_json(self.dir / "provenance.json", [{"role": "review"}, "x", {"role": 3, "model": "m"}, {"role": "review", "model": " "}])
        self.assertEqual(provenance.models(Item(self.dir)), {})


if __name__ == "__main__":
    unittest.main()
