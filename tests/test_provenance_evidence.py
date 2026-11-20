"""Attribution requires item-scoped work, not a path in shared session context."""
import json
import pathlib
import tempfile
import unittest

from factory.core import provenance
from factory.util import Item, write_json


class ProvenanceEvidence(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name).resolve()
        self.directory = self.root / "transcripts"
        self.directory.mkdir()
        self.itemdir = self.root / "runs/day/lexical"
        self.itemdir.mkdir(parents=True)
        write_json(self.itemdir / "item.json", {"repo": "org/lexical"})
        self.item = Item(self.itemdir)

    def transcript(self, name, prompt, *messages):
        rows = [{"role": "user", "content": prompt}, {"role": "assistant", "model": "route-" + name}, *messages]
        (self.directory / (name + ".jsonl")).write_text("".join(json.dumps({"message": row}) + "\n" for row in rows))

    def collect(self):
        diagnostics = {}
        result = provenance.collect(self.item, self.directory, diagnostics)
        return result, diagnostics["rejected"]

    def test_whole_session_does_not_attribute_scouts_from_shared_context(self):
        context = (f"<system-reminder>role=follow item={self.itemdir}\n"
                   f"bin/factory brief follow --item {self.itemdir}</system-reminder>\n")
        self.transcript("follow", context + f"bin/factory brief follow --item {self.itemdir}")
        for name in ("effect", "assistant-ui"):
            self.transcript(name, context + f"bin/factory brief scout --repo org/{name}\n"
                            f"Other worker owns {self.itemdir}; do not change its files.")
        rows, rejected = self.collect()
        self.assertEqual(rows, [{"role": "follow", "agent": "follow", "model": "route-follow"}])
        self.assertEqual({r["agent"] for r in rejected}, {"effect", "assistant-ui"})
        self.assertTrue(all("target differs" in r["reason"] for r in rejected))

    def test_loose_mention_without_item_assignment_is_rejected(self):
        self.transcript("scout", f"bin/factory brief scout --repo org/lexical\nCompare with {self.itemdir}.")
        rows, rejected = self.collect()
        self.assertEqual(rows, [])
        self.assertIn("mentions alone", rejected[0]["reason"])

    def test_explicit_repo_must_agree_with_item(self):
        self.transcript("wrong", f"bin/factory brief review --repo org/other --item {self.itemdir}")
        rows, rejected = self.collect()
        self.assertEqual(rows, [])
        self.assertIn("target differs", rejected[0]["reason"])

    def test_item_without_recognized_role_is_rejected(self):
        self.transcript("path", f"Check {self.itemdir}")
        rows, rejected = self.collect()
        self.assertEqual(rows, [])
        self.assertIn("no recognized role", rejected[0]["reason"])

    def test_item_file_work_in_matching_repo_is_evidence(self):
        for name in ("Write", "Edit"):
            self.transcript(name, "bin/factory brief scout --repo org/lexical",
                            {"role": "assistant", "content": [{"type": "tool_use", "name": name,
                             "input": {"file_path": str(self.itemdir / "demand.json")}}]})
        rows, rejected = self.collect()
        self.assertEqual({r["agent"] for r in rows}, {"Write", "Edit"})
        self.assertEqual(rejected, [])

    def test_file_reads_by_another_repo_or_item_do_not_count(self):
        for name, prompt in (("repo", "bin/factory brief scout --repo org/other"),
                             ("item", "bin/factory brief review --item runs/day/other")):
            self.transcript(name, prompt, {"role": "assistant", "content": [{"type": "tool_use", "name": "Read",
                            "input": {"file_path": str(self.itemdir / "item.json")}}]})
        rows, rejected = self.collect()
        self.assertEqual(rows, [])
        self.assertEqual(len(rejected), 2)

    def test_executed_item_brief_can_supply_assignment(self):
        self.transcript("review", "Review the assigned fix.",
                        {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash",
                         "input": {"command": f"bin/factory brief review --item {self.itemdir}"}}]})
        rows, rejected = self.collect()
        self.assertEqual(rows, [{"role": "review", "agent": "review", "model": "route-review"}])
        self.assertEqual(rejected, [])

    def test_printing_a_brief_is_not_executing_it(self):
        self.transcript("echo", "Inspect session logs.",
                        {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash",
                         "input": {"command": f"echo 'bin/factory brief review --item {self.itemdir}'"}}]})
        self.assertEqual(self.collect()[0], [])

    def test_tool_brief_switches_do_not_carry_previous_models(self):
        self.transcript("switch", "bin/factory brief scout --repo org/other",
                        {"role": "assistant", "model": "route-review", "content": [{"type": "tool_use", "name": "Bash",
                         "input": {"command": f"bin/factory brief review --item {self.itemdir}"}}]},
                        {"role": "assistant", "model": "route-other", "content": [{"type": "tool_use", "name": "Bash",
                         "input": {"command": "bin/factory brief review --item runs/day/other"}}]})
        self.assertEqual(self.collect()[0], [{"role": "review", "agent": "switch", "model": "route-review"}])

    def test_compound_briefs_cannot_borrow_an_item_from_echo(self):
        other = self.root / "runs/day/other"
        other.mkdir(parents=True)
        write_json(other / "item.json", {"repo": "org/lexical"})
        for index, separator in enumerate(("; ", " && ", "\n", " | ")):
            self.transcript(str(index), "Inspect candidates.",
                            {"role": "assistant", "model": "other-model", "content": [
                                {"type": "tool_use", "name": "Bash", "input": {"command":
                                 f"bin/factory brief review --item {other}{separator}echo --item {self.itemdir}"}}]})
        rows, rejected = self.collect()
        self.assertEqual(rows, [])
        self.assertEqual({r["agent"] for r in rejected}, {str(i) for i in range(4)})
        self.assertTrue(all(r["reason"] for r in rejected))

    def test_reference_reads_do_not_assign_repository_scouts(self):
        for name in ("item.json", "demand.json", "review.json"):
            self.transcript(name, "bin/factory brief scout --repo org/lexical\n"
                            "Find a new issue. Read an old item only as prior art; do not work on it.",
                            {"role": "assistant", "model": "unrelated-scout", "content": [
                                {"type": "tool_use", "name": "Read", "input": {"file_path": str(self.itemdir / name)}}]})
        rows, rejected = self.collect()
        self.assertEqual(rows, [])
        self.assertEqual(len(rejected), 3)
        self.assertTrue(all("mentions alone" in r["reason"] for r in rejected))

    def test_multiline_and_bare_path_assignments(self):
        for name, prompt in (("fields", f"role=review\nitem={self.itemdir}"),
                             ("bare", f"role=review Review {self.itemdir}"),
                             ("flags", f"role=review\n--item {self.itemdir}")):
            self.transcript(name, prompt)
        rows, rejected = self.collect()
        self.assertEqual({r["agent"] for r in rows}, {"fields", "bare", "flags"})
        self.assertTrue(all(r["role"] == "review" for r in rows))
        self.assertEqual(rejected, [])

    def test_line_continued_brief_is_one_invocation(self):
        command = f"bin/factory brief review \\\n --item {self.itemdir} \\\n --repo org/lexical"
        self.transcript("prompt", command)
        self.transcript("tool", "Inspect candidates.", {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": command}}]})
        rows, rejected = self.collect()
        self.assertEqual({r["agent"] for r in rows}, {"prompt", "tool"})
        self.assertEqual(rejected, [])

    def test_same_role_bare_path_switch_ends_previous_assignment(self):
        other = self.root / "runs/day/other"
        other.mkdir(parents=True)
        write_json(other / "item.json", {"repo": "org/lexical"})
        self.transcript("switch", f"bin/factory brief review --item {self.itemdir}",
                        {"role": "user", "content": f"role=review Review {other}"},
                        {"role": "assistant", "model": "other-model"})
        rows, rejected = self.collect()
        self.assertEqual(rows, [{"role": "review", "agent": "switch", "model": "route-switch"}])
        self.assertEqual(rejected[0]["reason"], "assigned to a different item")

    def test_explicit_other_item_cannot_borrow_a_reference_path(self):
        self.transcript("other", f"role=review item=/other/item; compare {self.itemdir} only as prior art.")
        rows, rejected = self.collect()
        self.assertEqual(rows, [])
        self.assertEqual(rejected[0]["reason"], "assigned to a different item")

    def test_bare_path_comparisons_do_not_override_the_assigned_item(self):
        other = self.root / "runs/day/other"
        for name, prompt in (("other", f"role=review Review {other}; compare {self.itemdir}"),
                             ("reference", f"role=scout Compare with {self.itemdir} as prior art.")):
            self.transcript(name, prompt)
        rows, rejected = self.collect()
        self.assertEqual(rows, [])
        self.assertEqual(len(rejected), 2)

    def test_metadata_edit_alone_is_not_item_output_evidence(self):
        self.transcript("metadata", "bin/factory brief scout --repo org/lexical",
                        {"role": "assistant", "content": [{"type": "tool_use", "name": "Edit",
                         "input": {"file_path": str(self.itemdir / "item.json")}}]})
        self.assertEqual(self.collect()[0], [])

    def test_claimed_creation_for_another_repository_is_rejected(self):
        self.transcript("other", "bin/factory brief scout --repo org/other",
                        {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "id": "new",
                         "input": {"command": "bin/factory new org/other#7"}}]},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "new",
                         "content": "item: " + str(self.itemdir)}]})
        rows, rejected = self.collect()
        self.assertEqual(rows, [])
        self.assertIn("target differs", rejected[0]["reason"])


if __name__ == "__main__":
    unittest.main()
