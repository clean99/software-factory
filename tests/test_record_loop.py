"""Generated destination bytes close their own pending record actions."""
import datetime as dt
import hashlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import setup_module
from factory import cli
from factory.core import ledger, nextaction, shiftlock
from factory.core.repos import canonical
from factory.oss import profile_readme, sync
from factory.util import CONFIG, write_json

AT = dt.datetime(2026, 11, 10, 12, tzinfo=dt.timezone.utc)
RECEIPT = {"at": AT.isoformat(), "ref": "existing", "sha256": "a" * 64}


def setUpModule():
    setup_module(unittest)


class RecordLoop(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name).resolve()
        self.ledger = self.root / "ledger/prs.json"
        self.ledger.parent.mkdir()
        (self.root / "runs").mkdir()
        write_json(self.ledger, [])
        plan = {key: [] for key in ("ship_ready", "needs_coordinator_review", "fix_round", "stale_reviews",
                                    "held", "unreviewed", "missing_clone", "errors", "repo_slots")}
        patches = [patch.dict(CONFIG, {"ledger_file": str(self.ledger), "runs_dir": str(self.root / "runs"),
                                       "repos_dir": str(self.root / "repos")}),
                   patch.object(nextaction, "ROOT", self.root), patch.object(shiftlock, "ROOT", self.root),
                   patch.object(nextaction.clean, "ROOT", self.root),
                   patch.object(sync, "ROOT", self.root), patch.object(profile_readme, "ROOT", self.root),
                   patch.object(nextaction, "now", return_value=AT), patch.object(sync, "now", return_value=AT),
                   patch.object(nextaction.plan, "run", return_value=plan),
                   patch.object(nextaction.shutil, "disk_usage", return_value=SimpleNamespace(free=20 * 1024 ** 3)),
                   patch.object(nextaction.lanes, "order", return_value=[]),
                   patch.object(nextaction.lessons, "lint", return_value=[]),
                   patch.object(nextaction.followup, "events", return_value=[]),
                   patch.object(nextaction.followup, "run", side_effect=AssertionError("no remote refresh")),
                   patch.object(nextaction.followup, "waiting_issues", side_effect=AssertionError("no remote issues")),
                   patch.object(nextaction, "run", return_value=SimpleNamespace(stdout="")),
                   patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocesses"))]
        for mocker in patches:
            mocker.start()
            self.addCleanup(mocker.stop)

    def row(self, number, repo="alpha/core", destination="profile", **extra):
        return {"repo": repo, "number": number, "url": f"https://github.com/{repo}/pull/{number}",
                "status": "merged", "submitted_at": "2026-11-01T00:00:00Z", "merged_at": "2026-11-09T00:00:00Z",
                "records": {d: RECEIPT for d in sync.DESTINATIONS if d != destination}, **extra}

    def run_cli(self, *args, expected_code=0):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(list(args))
        self.assertEqual(code, expected_code, errors.getvalue())
        return output.getvalue()

    def pending_records(self, destination):
        return [a for a in nextaction.actions(observe=False) if a["kind"] == "records" and a["target"] == destination]

    def generate(self, destination, rows):
        if destination == "profile":
            groups = self.root / "lines/oss/profile-groups.json"
            groups.parent.mkdir(parents=True, exist_ok=True)
            write_json(groups, {"groups": [{"title": "Merged", "repos": [
                {"repo": repo, "blurb": "Restore supported inputs"} for repo in sorted({canonical(r["repo"]) for r in rows})]}]})
            path = self.root / "profile.md"
            path.write_text("# Profile\n")
            self.run_cli("sync", "profile", "--readme", str(path), "--write")
            self.assertTrue(profile_readme.check(path.read_text(), CONFIG["github_user"])["ok"])
        elif destination == "teach":
            self.run_cli("sync", "interview", "--write")
            path = self.root / "docs/interview/CONTRIBUTIONS.md"
        else:
            directory = self.root / "outbox"
            self.run_cli("sync", "notebook", "--out", str(directory))
            path = directory / "status.md"
        return path

    def assert_closes(self, destination, rows):
        write_json(self.ledger, rows)
        owed = {r["url"] for r in rows if r["status"] == "merged" and not sync.real_receipt(sync.records(r).get(destination))}
        self.assertTrue(owed)
        self.assertEqual(len(self.pending_records(destination)), 1)
        first = json.loads(self.run_cli("next", "--json"))
        self.assertEqual((first["kind"], first["target"]), ("records", destination))
        path = self.generate(destination, rows)
        content = path.read_bytes()
        result = json.loads(self.run_cli("sync", "mark", destination, "--file", str(path)))
        self.assertEqual(result["missing"], [])
        self.assertEqual({r["url"] for r in result["marked"]}, owed)
        digest = hashlib.sha256(content).hexdigest()
        for saved in ledger.load():
            if saved["url"] in owed:
                self.assertEqual(saved["records"][destination]["sha256"], digest)
                self.assertEqual(saved["records"][destination]["ref"], path.name)
                self.assertTrue(sync.real_receipt(saved["records"][destination]))
                self.assertTrue(sync.complete(saved))
        self.assertEqual(self.pending_records(destination), [])
        self.assertEqual(sync.status()[destination], [])
        second = json.loads(self.run_cli("next", "--json"))
        self.assertNotEqual((second["kind"], second["target"]), ("records", destination))
        before = self.ledger.read_bytes()
        self.assertEqual(json.loads(self.run_cli("sync", "mark", destination, "--file", str(path))),
                         {"marked": [], "missing": []})
        self.assertEqual(self.ledger.read_bytes(), before)
        return path.read_text()

    def test_profile_single_and_aggregate_links_close_the_record_loop(self):
        rows = [self.row(7), self.row(8), self.row(21, "beta/kit")]
        text = self.assert_closes("profile", rows)
        self.assertIn("https://github.com/alpha/core/pulls?q=is%3Apr+author%3Afixture-owner+is%3Amerged) (2)", text)
        self.assertIn("https://github.com/beta/kit/pull/21", text)
        self.assertNotIn("https://github.com/alpha/core/pull/7", text)

    def test_profile_duplicate_alias_rows_share_the_generated_count_and_receipt(self):
        for schema in (1, 2):
            with self.subTest(schema=schema):
                write_json(self.ledger, [])
                item = self.root / "runs" / f"alias-{schema}"
                item.mkdir()
                write_json(item / "item.json", {"schema": schema, "repo": "meta/lexical"})
                for number, repo in ((7, "facebook/lexical"), (8, "meta/lexical"), (7, "meta/lexical")):
                    ledger.upsert(self.row(number, repo, item=str(item), notes=f"Keep {repo} #{number}"))
                rows = ledger.load()
                self.assertEqual(len(rows), 3)
                text = self.assert_closes("profile", rows)
                self.assertIn(profile_readme.merged_url("meta/lexical", CONFIG["github_user"]) + ") (2)", text)
                self.assertEqual([r["notes"] for r in ledger.load()], [r["notes"] for r in rows])

    def assert_manual_profile_preserved(self, path, manual):
        text = path.read_text()
        header, marker, remainder = text.partition(profile_readme.PORTFOLIO_END + "\n\n")
        self.assertTrue(header.startswith(profile_readme.PORTFOLIO_START + "\n"))
        self.assertIn("This account is automated", header)
        self.assertEqual(marker, profile_readme.PORTFOLIO_END + "\n\n")
        self.assertTrue(remainder.startswith(manual))
        self.assertEqual(text.count(manual), 1)

    def test_outside_block_requires_manual_pull_references_before_the_loop_can_close(self):
        old_aggregate = profile_readme.merged_url("facebook/lexical", "fixture-owner")
        cases = [("alpha/core", "alpha/core", "[Tool](https://github.com/alpha/core)"),
                 ("facebook/lexical", "meta/lexical", f"[Tool]({old_aggregate}) (2)")]
        for schema in (1, 2):
            for old_repo, repo, reference in cases:
                with self.subTest(schema=schema, repo=repo):
                    item = self.root / "runs" / f"manual-{schema}-{repo.replace('/', '-')}"
                    item.mkdir()
                    write_json(item / "item.json", {"schema": schema, "repo": repo})
                    rows = [self.row(7, old_repo, item=str(item)), self.row(8, repo, item=str(item))]
                    write_json(self.ledger, rows)
                    groups = self.root / "lines/oss/profile-groups.json"
                    groups.parent.mkdir(parents=True, exist_ok=True)
                    write_json(groups, {"groups": [{"title": "Merged", "repos": [
                        {"repo": repo, "blurb": "Manual entry", "outside_block": True}]}]})
                    path = self.root / "manual-profile.md"
                    original = f"# Profile\nBuilding: {reference}\n"
                    path.write_text(original)
                    first = json.loads(self.run_cli("next", "--json"))
                    self.assertEqual((first["kind"], first["target"]), ("records", "profile"))
                    with self.assertRaisesRegex(SystemExit, "add .* to the hand-maintained entry for"):
                        self.run_cli("sync", "profile", "--readme", str(path), "--write")
                    self.assertEqual(path.read_text(), original)
                    result = json.loads(self.run_cli("sync", "mark", "profile", "--file", str(path), expected_code=1))
                    self.assertEqual(result, {"marked": [], "missing": [r["url"] for r in rows]})
                    self.assertEqual(json.loads(self.run_cli("next", "--json")), first)
                    manual = "# Profile\nBuilding: " + " and ".join(f"[Fix]({r['url']})" for r in rows) + "\n"
                    path.write_text(manual)
                    self.run_cli("sync", "profile", "--readme", str(path), "--write")
                    self.assert_manual_profile_preserved(path, manual)
                    self.assertEqual(profile_readme.block(path.read_text()), "\n\n")
                    checked = profile_readme.check(path.read_text(), CONFIG["github_user"])
                    self.assertTrue(checked["ok"], checked)
                    self.assertEqual(checked["unrecorded_prs"], [])
                    result = json.loads(self.run_cli("sync", "mark", "profile", "--file", str(path)))
                    self.assertEqual(result["missing"], [])
                    self.assertEqual({r["url"] for r in result["marked"]}, {r["url"] for r in rows})
                    for saved in ledger.load():
                        self.assertEqual(saved["records"]["profile"]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                        self.assertTrue(sync.complete(saved))
                    self.assertEqual(self.pending_records("profile"), [])
                    following = json.loads(self.run_cli("next", "--json"))
                    self.assertNotEqual((following["kind"], following["target"]), ("records", "profile"))

    def test_profile_preserves_a_single_hand_maintained_pull_link(self):
        rows = [self.row(7), self.row(21, "beta/kit")]
        write_json(self.ledger, rows)
        groups = self.root / "lines/oss/profile-groups.json"
        groups.parent.mkdir(parents=True)
        write_json(groups, {"groups": [{"title": "Merged", "repos": [
            {"repo": "alpha/core", "blurb": "Manual entry", "outside_block": True},
            {"repo": "beta/kit", "blurb": "Restore supported inputs"}]}]})
        path = self.root / "profile.md"
        manual = f"# Profile\nBuilding: regression tooling, proven by [Core]({rows[0]['url']}).\n"
        path.write_text(manual)
        self.assertEqual(len(self.pending_records("profile")), 1)
        self.run_cli("sync", "profile", "--readme", str(path), "--write")
        self.assert_manual_profile_preserved(path, manual)
        self.assertNotIn("alpha/core", profile_readme.block(path.read_text()))
        checked = profile_readme.check(path.read_text(), CONFIG["github_user"])
        self.assertTrue(checked["ok"], checked)
        result = json.loads(self.run_cli("sync", "mark", "profile", "--file", str(path)))
        self.assertEqual(result["missing"], [])
        self.assertEqual({r["url"] for r in result["marked"]}, {r["url"] for r in rows})
        self.assertTrue(all(sync.complete(saved) for saved in ledger.load()))
        self.assertEqual(self.pending_records("profile"), [])

    def test_interview_contribution_table_closes_the_record_loop(self):
        rows = [self.row(7, destination="teach"), self.row(8, destination="teach"),
                self.row(21, "beta/kit", destination="teach")]
        text = self.assert_closes("teach", rows)
        for row in rows:
            self.assertTrue(sync.mentioned(text, row))
            self.assertIn(f"[PR]({row['url']})", text)

    def test_notebook_status_closes_the_record_loop(self):
        rows = [self.row(7, destination="notebook"), self.row(8, destination="notebook")]
        text = self.assert_closes("notebook", rows)
        self.assertIn("2026-11-09 alpha/core merged: #7, #8", text)

    def test_bounded_notebook_status_keeps_every_owed_merge(self):
        recorded = [self.row(n, repo=f"owner/project-{n:03d}", records={d: RECEIPT for d in sync.DESTINATIONS})
                    for n in range(160)]
        owed = [self.row(7, destination="notebook"), self.row(8, destination="notebook"),
                self.row(9, "pending/old", destination="notebook", merged_at="2026-09-01T00:00:00Z"),
                self.row(10, "pending/undated", destination="notebook", merged_at=None)]
        text = self.assert_closes("notebook", [*recorded, *owed])
        self.assertLessEqual(len(text), 6000)
        self.assertIn("omitted", text)
        for row in owed:
            self.assertTrue(sync.mentioned(text, row))

    def test_stale_aggregate_count_keeps_the_record_action_pending(self):
        rows = [self.row(7), self.row(8)]
        write_json(self.ledger, rows)
        path = self.generate("profile", rows)
        path.write_text(path.read_text().replace(" (2)", " (1)"))
        result = json.loads(self.run_cli("sync", "mark", "profile", "--file", str(path), expected_code=1))
        self.assertEqual(result, {"marked": [], "missing": [r["url"] for r in rows]})
        self.assertEqual(len(self.pending_records("profile")), 1)

    def test_unrelated_references_never_close_explicit_or_batch_records(self):
        texts = ["Recorded: beta/kit #7; alpha/core #8.",
                 "https://github.com/beta/kit/pull/7; https://github.com/alpha/core/pull/8",
                 "https://github.com/alpha/core/issues/7", "xalpha/core #7", "alpha/core #70",
                 "https://github.com/alpha/core/pull/70", "#7 alpha/core"]
        path = self.root / "read-back.txt"
        for destination in sync.DESTINATIONS:
            for text in texts:
                with self.subTest(destination=destination, text=text):
                    current = self.row(7, destination=destination)
                    write_json(self.ledger, [current])
                    path.write_text(text)
                    before = self.ledger.read_bytes()
                    with self.assertRaisesRegex(SystemExit, "does not mention"):
                        self.run_cli("sync", "mark", destination, current["url"], "--file", str(path))
                    self.assertEqual(self.ledger.read_bytes(), before)
                    result = json.loads(self.run_cli("sync", "mark", destination, "--file", str(path), expected_code=1))
                    self.assertEqual(result, {"marked": [], "missing": [current["url"]]})
                    self.assertEqual(self.ledger.read_bytes(), before)
                    self.assertEqual(len(self.pending_records(destination)), 1)
                    self.assertFalse(sync.complete(ledger.load()[0]))


if __name__ == "__main__":
    unittest.main()
