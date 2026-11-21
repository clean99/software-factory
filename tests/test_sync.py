"""Local record generation and destination receipt contracts."""
import argparse
import concurrent.futures
import datetime as dt
import hashlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import setup_module
from factory import cli, util
from factory.core import clones, ledger
from factory.oss import profile_readme, retro, sync
from factory.util import CONFIG, write_json

AT = dt.datetime(2026, 11, 10, 12, tzinfo=dt.timezone.utc)
RECEIPT = {"at": AT.isoformat(), "ref": "checked", "sha256": "a" * 64}


def setUpModule():
    setup_module(unittest)


def row(number=1, repo="alpha/core", status="merged", **fields):
    return {"repo": repo, "number": number, "url": f"https://github.com/{repo}/pull/{number}",
            "status": status, "submitted_at": "2026-11-01T12:00:00Z", **fields}


def groups():
    return {"groups": [{"title": "Merged upstream", "repos": [
        {"repo": "alpha/core", "label": "Alpha", "blurb": "keep results"},
        {"repo": "beta/kit", "label": "Beta", "blurb": "decode documents"},
        {"repo": "meta/lexical", "label": "Lexical", "blurb": "round-trip links"}]}]}


class Workspace(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.ledger_path = self.root / "prs.json"
        write_json(self.ledger_path, [])
        self.highlights = self.root / 'lines/oss/profile-highlights.json'
        self.highlights.parent.mkdir(parents=True)
        write_json(self.highlights, {'active_contributor': {'minimum_merged': 5, 'limit': 6}, 'selected': []})
        for mocker in (patch.dict(CONFIG, {"ledger_file": str(self.ledger_path), "repos_dir": str(self.root / "repos")}),
                       patch.object(clones, "ensure_repo", side_effect=AssertionError("unexpected clone restore")),
                       patch.object(sync, "ROOT", self.root),
                       patch.object(profile_readme, "ROOT", self.root),
                       patch.object(sync, "now", return_value=AT),
                       patch.object(util, "run", side_effect=AssertionError("subprocess")),
                       patch.object(retro, "gh_json", side_effect=AssertionError("network")),
                       patch.object(profile_readme, "gh_json", side_effect=AssertionError("network"))):
            mocker.start()
            self.addCleanup(mocker.stop)
        if not hasattr(ledger, "update_fields"):
            def update(url, **fields):
                rows = ledger.load()
                current = ledger.find(rows, url)
                current.update(fields)
                write_json(self.ledger_path, rows)
                return current
            mocker = patch.object(ledger, "update_fields", side_effect=update, create=True)
            mocker.start()
            self.addCleanup(mocker.stop)

    def save(self, rows):
        write_json(self.ledger_path, rows)

    def cli(self, *argv, expected_code=0):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(required=True)
        sync.register(sub)
        args = parser.parse_args(["sync", *argv])
        out, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(errors):
            result = args.fn(args)
        self.assertEqual(result, expected_code, errors.getvalue())
        return out.getvalue()

    def item(self, name, text):
        path = self.root / "runs" / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "pr.md").write_text(text)
        (path / "item.json").write_text('{"status": "submitted", "title": "not the PR title"}')
        return str(path)

    def group_file(self, data=None):
        path = self.root / "lines/oss/profile-groups.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, data or groups())


class Receipts(Workspace):
    def test_status_lists_merged_only_and_unverified_last(self):
        legacy = row(1, recorded_at="2026-11-02T00:00:00Z")
        missing = row(2)
        deferred = row(3, records={"profile": {"deferred": "later", "at": "today"}, "teach": RECEIPT})
        done = row(4, records={d: RECEIPT for d in sync.DESTINATIONS})
        self.save([legacy, done, missing, deferred, row(5, status="closed"), row(6, status="submitted")])
        result = json.loads(self.cli("status"))
        self.assertEqual(result["profile"], [missing["url"], deferred["url"], legacy["url"]])
        self.assertEqual(result["teach"], [missing["url"], legacy["url"]])
        self.assertEqual(result["notebook"], result["profile"])
        self.assertTrue(sync.complete(done))
        for r in (legacy, missing, deferred):
            self.assertFalse(sync.complete(r))
        self.assertNotIn("records", ledger.load()[0])

    def test_real_receipts_need_fields_and_deferred_is_not_complete(self):
        for receipt in ({}, {"at": "today"}, {"at": "today", "ref": "checked"},
                        {**RECEIPT, "deferred": "later"}, {**RECEIPT, "unverified": True}, None):
            self.assertFalse(sync.real_receipt(receipt))
        for digest in ("", "a" * 63, "a" * 65, "g" * 64, "a" * 64 + "\n", None, 123):
            with self.subTest(digest=digest):
                self.assertFalse(sync.real_receipt({**RECEIPT, "sha256": digest}))
        self.assertTrue(sync.real_receipt(RECEIPT))
        self.assertTrue(sync.real_receipt({**RECEIPT, "sha256": "B" * 64}))
        self.assertEqual(sync.records(row(records={}, recorded_at="old")), {})

    def test_malformed_destination_receipts_remain_owed(self):
        self.save([row(1, records={"profile": None}), row(2, records={"profile": "not a receipt"})])
        self.assertEqual(sync.status()["profile"], [row(1)["url"], row(2)["url"]])
        self.assertTrue(all(not sync.complete(r) for r in ledger.load()))

    def test_mark_migrates_other_legacy_receipts_and_preserves_owner_fields(self):
        r = row(recorded_at="old", notes="keep", adopted_in="upstream")
        self.save([r])
        path = self.root / "README.md"
        content = (r["url"] + "\r\nKeep \\ literal text.\r\n").encode()
        path.write_bytes(content)
        result = json.loads(self.cli("mark", "profile", r["url"], "--file", str(path), "--ref", "revision-3"))
        self.assertEqual(set(result), {"at", "ref", "sha256"})
        self.assertEqual(result["ref"], "revision-3")
        self.assertEqual(result["sha256"], hashlib.sha256(content).hexdigest())
        current = ledger.load()[0]
        self.assertEqual(current["notes"], "keep")
        self.assertEqual(current["adopted_in"], "upstream")
        self.assertEqual(current["records"]["teach"], {"at": "old", "unverified": True})
        self.assertEqual(current["records"]["notebook"], current["records"]["teach"])
        self.assertNotIn(r["url"], sync.status()["profile"])
        self.cli("mark", "notebook", r["url"], "--deferred", "connector unavailable")
        self.assertEqual(ledger.load()[0]["records"]["notebook"]["deferred"], "connector unavailable")
        self.assertIn(r["url"], sync.status()["notebook"])

    def test_concurrent_marks_preserve_all_destinations(self):
        r = row(recorded_at="old")
        self.save([r])
        path = self.root / "destination.md"
        path.write_text(r["url"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda dest: sync.mark(dest, r["url"], file=path, ref=dest), sync.DESTINATIONS))
        self.assertTrue(sync.complete(ledger.load()[0]))
        self.assertEqual({d: receipt["ref"] for d, receipt in ledger.load()[0]["records"].items()},
                         {d: d for d in sync.DESTINATIONS})

    def test_mark_requires_read_back_text_and_rejects_caller_hashes(self):
        self.save([row()])
        path = self.root / "destination.md"
        path.write_text(row()["url"])
        for argv in (("profile", row()["url"], "--ref", "revision"),
                     ("profile", row()["url"], "--file", str(path), "--sha256", "a" * 64),
                     ("notebook", row()["url"], "--deferred", " "),
                     ("notebook", "--deferred", "later"),
                     ("profile", row()["url"], "--deferred", "later"),
                     ("notebook", row()["url"], "--deferred", "later", "--ref", "revision"),
                     ("notebook", row()["url"], "--deferred", "later", "--file", str(path))):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.cli("mark", *argv)
        self.assertNotIn("records", ledger.load()[0])

    def test_explicit_mark_refuses_missing_pr_or_absent_mention_without_writes(self):
        self.save([row()])
        path = self.root / "destination.md"
        before = self.ledger_path.read_bytes()
        for content in ("alpha/core #10", "beta/kit #1", "xalpha/core #1", "No PR here"):
            path.write_text(content)
            with self.subTest(content=content), self.assertRaisesRegex(SystemExit, "does not mention.*--file"):
                self.cli("mark", "profile", row()["url"], "--file", str(path))
            self.assertEqual(self.ledger_path.read_bytes(), before)
        path.write_text(row()["url"])
        with self.assertRaisesRegex(SystemExit, "not in the ledger"):
            self.cli("mark", "profile", row(99)["url"], "--file", str(path))
        with self.assertRaisesRegex(SystemExit, "does not exist"):
            self.cli("mark", "profile", row()["url"], "--file", str(self.root / "missing"))
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_mark_matches_aliases_and_defaults_ref_to_the_matching_filename(self):
        current = row(7, "facebook/lexical")
        self.save([current])
        folder = self.root / "read-back"
        folder.mkdir()
        (folder / "first.md").write_text("No PR here")
        path = folder / "note.txt"
        path.write_text("meta/lexical #7")
        result = json.loads(self.cli("mark", "notebook", current["url"], "--file", str(folder)))
        self.assertEqual(result["ref"], path.name)
        self.assertEqual(result["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_batch_marks_only_mentioned_merged_prs_still_owed(self):
        rows = [row(1, records={"teach": RECEIPT}), row(2, recorded_at="old"), row(3),
                row(4, records={"profile": RECEIPT}), row(5, status="submitted"), row(6, status="closed"),
                row(7, records={"profile": {**RECEIPT, "sha256": ""}})]
        self.save(rows)
        folder = self.root / "read-back"
        folder.mkdir()
        (folder / "ignored.json").write_text("alpha/core #3")
        (folder / "first.md").write_text("alpha/core #1\nalpha/core #4\nalpha/core #5\nalpha/core #6")
        nested = folder / "nested"
        nested.mkdir()
        (nested / "second.txt").write_text("alpha/core #2\nalpha/core #7")
        result = json.loads(self.cli("mark", "profile", "--file", str(folder), expected_code=1))
        self.assertEqual(result["missing"], [rows[2]["url"]])
        self.assertEqual([entry["url"] for entry in result["marked"]], [rows[i]["url"] for i in (0, 1, 6)])
        saved = ledger.load()
        for index, path in ((0, folder / "first.md"), (1, nested / "second.txt"), (6, nested / "second.txt")):
            receipt = saved[index]["records"]["profile"]
            self.assertEqual(receipt["ref"], path.name)
            self.assertEqual(receipt["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(saved[0]["records"]["teach"], RECEIPT)
        self.assertEqual(saved[2:6], rows[2:6])
        self.assertEqual(sync.status()["profile"], [rows[2]["url"]])
        before = self.ledger_path.read_bytes()
        second = json.loads(self.cli("mark", "profile", "--file", str(folder), expected_code=1))
        self.assertEqual(second, {"marked": [], "missing": [rows[2]["url"]]})
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_partial_batch_returns_failure_with_repairs_and_keeps_saved_receipts(self):
        rows = [row(7, "facebook/lexical"), row(8, "meta/lexical"), row(7, "meta/lexical"), row(21, "beta/kit")]
        path = self.root / "read back.txt"
        path.write_text(rows[-1]["url"] + "\n")
        for destination in sync.DESTINATIONS:
            with self.subTest(destination=destination):
                self.save(rows)
                out, errors = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(errors):
                    code = cli.main(["sync", "mark", destination, "--file", str(path)])
                result = json.loads(out.getvalue())
                self.assertEqual(result["missing"], [r["url"] for r in rows[:-1]])
                self.assertEqual([r["url"] for r in result["marked"]], [rows[-1]["url"]])
                saved = ledger.load()
                self.assertEqual(saved[:-1], rows[:-1])
                self.assertEqual(saved[-1]["records"][destination]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(code, 1)
                messages = errors.getvalue().splitlines()
                self.assertEqual(len(messages), len(result["missing"]))
                for current, message in zip(rows[:-1], messages):
                    self.assertIn(current["url"], message)
                    self.assertIn(f'meta/lexical #{current["number"]}', message)
                    self.assertIn("on one line", message)
                    if destination == "profile":
                        aggregate = profile_readme.merged_url("meta/lexical", CONFIG["github_user"])
                        self.assertIn(f"[meta/lexical]({aggregate}) (2)", message)
                        self.assertIn(f"bin/factory sync profile --readme '{path}' --write", message)
                        self.assertIn("regenerates the block", message)
                    else:
                        self.assertNotIn("sync profile", message)
                        self.assertNotIn("/pulls?", message)
                path.write_text("\n".join(r["url"] for r in rows))
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as complete_errors:
                    code = cli.main(["sync", "mark", destination, "--file", str(path)])
                self.assertEqual(code, 0)
                self.assertEqual(complete_errors.getvalue(), "")
                self.assertEqual(sync.status()[destination], [])
                path.write_text(rows[-1]["url"] + "\n")

    def test_batch_mark_requires_all_source_text_readable_before_writing(self):
        self.save([row()])
        folder = self.root / "read-back"
        folder.mkdir()
        (folder / "first.md").write_text(row()["url"])
        (folder / "second.txt").write_bytes(b"\xff")
        before = self.ledger_path.read_bytes()
        with self.assertRaises(SystemExit):
            self.cli("mark", "teach", "--file", str(folder))
        self.assertEqual(self.ledger_path.read_bytes(), before)


class Mentions(Workspace):
    def test_numbers_belong_to_the_nearest_preceding_repo_on_the_same_line(self):
        cases = {
            "alpha/core #7": {7},
            "alpha/core#7": {7},
            "| alpha/core | #7 | merged |": {7},
            "2026-11-10 alpha/core merged: #7, #8": {7, 8},
            "Recorded: beta/kit #7; alpha/core #8.": {8},
            "alpha/core #7; beta/kit #8": {7},
            "alpha/core\n#7": set(),
            "#7 alpha/core": set(),
            "| #7 | alpha/core |": set(),
            "alpha/core #70": set(),
            "alpha/core #7more": set(),
            "alpha/core #7_draft": set(),
            "alpha/core #7.": {7},
            "alpha/core (#7)": {7},
            "ALPHA/CORE merged: #7": {7},
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                actual = {n for n in (7, 8) if sync.mentioned(text, row(n))}
                self.assertEqual(actual, expected)

    def test_pull_urls_and_repo_tokens_do_not_match_paths_or_issues(self):
        for text in ("[PR](https://github.com/alpha/core/pull/7)",
                     "https://github.com/ALPHA/CORE/pull/7/files",
                     "https://github.com/alpha/core/pull/7?diff=split",
                     "https://github.com/alpha/core/pull/7#discussion"):
            with self.subTest(text=text):
                self.assertTrue(sync.mentioned(text, row(7)))
        for text in ("https://github.com/alpha/core/pull/70", "https://github.com/alpha/core/pull/7oops",
                     "https://github.com/alpha/core/issues/7", "xalpha/core #7", "alpha/core-tools #7",
                     "path/alpha/core #7", "alpha/core/path #7", "https://alpha/core#7",
                     "https://example.test/?repo=alpha/core#7",
                     "https://example.test/github.com/alpha/core/pull/7"):
            with self.subTest(text=text):
                self.assertFalse(sync.mentioned(text, row(7)))

    def test_former_and_current_repo_names_match_in_both_directions(self):
        for repo in ("facebook/lexical", "META/LEXICAL"):
            for name in ("FACEBOOK/LEXICAL", "meta/lexical"):
                for text in (f"{name} merged: #7, #8", f"https://github.com/{name}/pull/7"):
                    with self.subTest(repo=repo, text=text):
                        self.assertTrue(sync.mentioned(text, row(7, repo)))

    def test_adjacent_references_cannot_discharge_a_different_pr(self):
        target = row(7)
        others = [row(7, "beta/kit"), row(8)]
        path = self.root / "read-back.txt"
        for text in ("Recorded: beta/kit #7; alpha/core #8.", "beta/kit #7, alpha/core #8",
                     "| beta/kit | #7 | alpha/core | #8 |"):
            path.write_text(text)
            for destination in sync.DESTINATIONS:
                with self.subTest(text=text, destination=destination):
                    self.save([target, *others])
                    before = self.ledger_path.read_bytes()
                    with self.assertRaisesRegex(SystemExit, "does not mention"):
                        self.cli("mark", destination, target["url"], "--file", str(path))
                    self.assertEqual(self.ledger_path.read_bytes(), before)
                    result = json.loads(self.cli("mark", destination, "--file", str(path), expected_code=1))
                    self.assertEqual(result["missing"], [target["url"]])
                    self.assertEqual({r["url"] for r in result["marked"]}, {r["url"] for r in others})
                    self.assertFalse(sync.complete(ledger.find(ledger.load(), target["url"])))
                    self.assertEqual(sync.status()[destination], [target["url"]])

    def test_aggregate_receipts_are_profile_only_and_use_all_merged_rows(self):
        rows = [row(7, "facebook/lexical", records={"profile": RECEIPT}), row(8, "META/LEXICAL"),
                row(9, "meta/lexical", status="submitted")]
        self.save(rows)
        path = self.root / "profile.md"
        path.write_text(profile_readme.render(rows, groups()))
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        for destination in ("teach", "notebook"):
            result = json.loads(self.cli("mark", destination, "--file", str(path), expected_code=1))
            self.assertEqual(result["marked"], [])
            self.assertEqual(result["missing"], [r["url"] for r in rows[:2]])
        result = json.loads(self.cli("mark", "profile", "--file", str(path)))
        self.assertEqual(result["missing"], [])
        self.assertEqual([r["url"] for r in result["marked"]], [rows[1]["url"]])
        self.assertEqual(result["marked"][0]["sha256"], expected)
        self.assertEqual(ledger.load()[0]["records"]["profile"], RECEIPT)
        self.assertNotIn("records", ledger.load()[2])
        with self.assertRaisesRegex(SystemExit, "does not mention"):
            self.cli("mark", "profile", rows[2]["url"], "--file", str(path))

    def test_stale_aggregate_count_overrides_individual_profile_mentions(self):
        rows = [row(7), row(8), row(9, "beta/kit")]
        self.save(rows)
        text = profile_readme.render(rows, groups())
        path = self.root / "profile.md"
        for count in (1, 3, 20):
            with self.subTest(count=count):
                self.save(rows)
                path.write_text(text.replace(" (2)", f" ({count})") + "\nalpha/core #7, #8\n")
                before = self.ledger_path.read_bytes()
                with self.assertRaisesRegex(SystemExit, "does not mention"):
                    self.cli("mark", "profile", rows[0]["url"], "--file", str(path))
                self.assertEqual(self.ledger_path.read_bytes(), before)
                result = json.loads(self.cli("mark", "profile", "--file", str(path), expected_code=1))
                self.assertEqual(result["missing"], [r["url"] for r in rows[:2]])
                self.assertEqual([r["url"] for r in result["marked"]], [rows[2]["url"]])

    def test_aggregate_link_requires_the_generator_query_and_adjacent_count(self):
        rows = [row(7), row(8)]
        text = profile_readme.render(rows, groups())
        path = self.root / "profile.md"
        for invalid in (text.replace("fixture-owner", "other-owner"), text.replace("is%3Amerged", "is%3Aopen"),
                        text.replace(" (2)", "\n(2)"), text.replace(" (2)", " total (2)"),
                        text.replace("alpha/core", "xalpha/core"), text.replace("[Alpha]", "Alpha]")):
            with self.subTest(text=invalid):
                self.save(rows)
                path.write_text(invalid)
                result = json.loads(self.cli("mark", "profile", "--file", str(path), expected_code=1))
                self.assertEqual(result["marked"], [])
                self.assertEqual(result["missing"], [r["url"] for r in rows])


class Profile(Workspace):
    def test_ordinals_include_teens(self):
        self.assertEqual([profile_readme.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21)],
                         ['1st', '2nd', '3rd', '4th', '11th', '12th', '13th', '21st'])

    def test_header_reads_latest_rank_snapshot_and_orders_counts_then_labels(self):
        self.group_file()
        rows = [row(n, repo) for repo, count in (('alpha/core', 6), ('beta/kit', 5), ('meta/lexical', 5)) for n in range(1, count + 1)]
        history = self.ledger_path.with_name('influence.jsonl')
        latest = {'at': '2026-11-20T12:00:00Z', 'repos': [
            {'repo': 'alpha/core', 'rank': 7, 'contributors_seen': 344},
            {'repo': 'beta/kit', 'rank': 28, 'contributors_seen': 428}]}
        history.write_text(json.dumps({'at': '2026-11-19', 'repos': []}) + '\n' + json.dumps(latest) + '\n\n')
        settings = {'active_contributor': {'minimum_merged': 5, 'limit': 2}, 'selected': []}
        write_json(self.highlights, settings)
        text = profile_readme.portfolio(rows, 'fixture-owner')
        self.assertIn(f"[Alpha]({profile_readme.merged_url('alpha/core', 'fixture-owner')}) (6 merged, 7th of 344 contributors)", text)
        self.assertIn(f"[Beta]({profile_readme.merged_url('beta/kit', 'fixture-owner')}) (5, 28th of 428)", text)
        self.assertNotIn('[Lexical]', text)
        self.assertIn("Ranks are by commits in GitHub's contributor list on 2026-11-20.", text)
        self.assertNotIn('2026-11-19', text)
        self.assertNotIn('Selected pull requests', text)

    def test_active_label_overrides_groups_without_changing_merged_block(self):
        self.group_file()
        rows = [row(n) for n in range(1, 6)]
        write_json(self.highlights, {'active_contributor': {'minimum_merged': 5, 'limit': 6,
                   'labels': {'alpha/core': 'Core project'}}, 'selected': []})
        text = profile_readme.portfolio(rows, 'fixture-owner')
        self.assertIn('**Active contributor to** [Core project]', text)
        self.assertIn('- [Alpha]', profile_readme.render(rows))
        for labels in ([], {'Alpha/Core': 'Core'}, {'alpha/core': ''}, {'alpha/core': 5}):
            write_json(self.highlights, {'active_contributor': {'minimum_merged': 5, 'limit': 6, 'labels': labels}, 'selected': []})
            with self.subTest(labels=labels), self.assertRaisesRegex(ValueError, 'labels must map'):
                profile_readme.load_highlights()

    def test_header_missing_rank_and_missing_influence_file_show_counts_only(self):
        self.group_file()
        rows = [row(n) for n in range(1, 6)] + [row(n, 'beta/kit') for n in range(1, 6)]
        text = profile_readme.portfolio(rows, 'fixture-owner')
        self.assertIn(') (5 merged) · [Beta]', text)
        self.assertNotIn('Ranks are', text)
        self.ledger_path.with_name('influence.jsonl').write_text(json.dumps({'at': '2026-11-20', 'repos': [
            {'repo': 'alpha/core', 'rank': 2, 'contributors_seen': 20}, {'repo': 'beta/kit', 'rank': None, 'contributors_seen': 40}]}) + '\n')
        text = profile_readme.portfolio(rows, 'fixture-owner')
        self.assertIn('(5 merged, 2nd of 20 contributors)', text)
        self.assertIn(') (5). Ranks are', text)
        self.assertNotIn('None', text)

    def test_header_omits_unqualified_projects_and_uses_fallback_labels(self):
        self.group_file({'groups': []})
        self.assertNotIn('Active contributor to', profile_readme.portfolio([row()], 'fixture-owner'))
        text = profile_readme.portfolio([row(n) for n in range(1, 6)], 'fixture-owner')
        self.assertIn('**Active contributor to** [core]', text)

    def test_highlights_keep_file_order_and_require_merged_upstream_rows(self):
        entries = [{'repo': 'beta/kit', 'number': 21, 'text': 'Decode inputs'},
                   {'repo': 'alpha/core', 'number': 7, 'text': 'Keep results'}]
        write_json(self.highlights, {'active_contributor': {'minimum_merged': 5, 'limit': 6}, 'selected': entries})
        rows = [row(7), row(21, 'beta/kit')]
        text = profile_readme.portfolio(rows, 'fixture-owner')
        self.assertIn('- [kit#21](https://github.com/beta/kit/pull/21): Decode inputs\n- [core#7]', text)
        for status in ('closed', 'submitted'):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, r'beta/kit#21.*not a merged upstream'):
                profile_readme.portfolio([row(7), row(21, 'beta/kit', status)], 'fixture-owner')
        with self.assertRaisesRegex(ValueError, r'beta/kit#21'):
            profile_readme.portfolio(rows, 'beta')

    def test_highlights_file_reports_missing_and_invalid_entries(self):
        self.highlights.unlink()
        with self.assertRaisesRegex(ValueError, 'restore a valid highlights file'):
            profile_readme.load_highlights()
        for entry in ({'repo': 'bad', 'number': 1, 'text': 'Fix'}, {'repo': 'alpha/core', 'number': True, 'text': 'Fix'}):
            write_json(self.highlights, {'active_contributor': {'minimum_merged': 5, 'limit': 6}, 'selected': [entry]})
            with self.assertRaisesRegex(ValueError, 'entry'):
                profile_readme.load_highlights()

    def test_header_dates_ignore_owned_rows_and_omit_missing_dates(self):
        rows = [row(1, submitted_at='2026-11-02T00:00:00Z'), row(2, submitted_at='2026-11-01T23:00:00Z'),
                row(1, 'fixture-owner/own', submitted_at='2020-01-01T00:00:00Z')]
        text = profile_readme.portfolio(rows, 'fixture-owner')
        self.assertIn('Since 2026-11-01 this account', text)
        self.assertIn('opened     2  since 2026-11-01', text)
        self.assertIn('merged     2  in 1 projects', text)
        text = profile_readme.portfolio([row(submitted_at=None)], 'fixture-owner')
        self.assertIn('**All in on AI.** This account', text)
        self.assertNotIn('Since ', text)

    def test_details_wrapper_and_hand_text_survive_two_identical_renders(self):
        self.group_file()
        rows = [row()]
        original = ('# Profile\n\n<details>\n<summary>Merged contributions by project</summary>\n\n'
                    + profile_readme.START + '\n' + profile_readme.END + '\n\n</details>\n\nWriting stays.\n')
        rendered = profile_readme.render_readme(original, rows)
        self.assertEqual(profile_readme.render_readme(rendered, rows), rendered)
        manual = rendered.split(profile_readme.PORTFOLIO_END, 1)[1]
        self.assertLess(manual.index('<details>'), manual.index(profile_readme.START))
        self.assertLess(manual.index(profile_readme.END), manual.index('</details>'))
        self.assertTrue(rendered.endswith('\n\n</details>\n\nWriting stays.\n'))

    def test_existing_portfolio_keeps_all_outside_marker_bytes(self):
        self.group_file()
        before = '# Personal heading\n\n  Keep this whitespace.\n\n'
        between = '\n\nPersonal introduction.\n\n'
        after = '\n\nWriting stays.\n'
        original = (before + profile_readme.PORTFOLIO_START + '\nOld header\n' + profile_readme.PORTFOLIO_END
                    + between + profile_readme.START + '\nOld list\n' + profile_readme.END + after)
        rendered = profile_readme.render_readme(original, [row()])
        self.assertEqual(rendered.split(profile_readme.PORTFOLIO_START)[0], before)
        self.assertEqual(rendered.split(profile_readme.PORTFOLIO_END)[1].split(profile_readme.START)[0], between)
        self.assertEqual(rendered.split(profile_readme.END)[1], after)
        self.assertEqual(profile_readme.render_readme(rendered, [row()]), rendered)

    def test_status_card_is_delegated_to_the_public_projection(self):
        rows = [row()]
        data = {'activity': {'days': []}}
        card = 'software-factory\nsnapshot fixture\n'
        with patch.object(profile_readme, 'portfolio_data', return_value=data) as projection, \
                patch.object(profile_readme.metrics_site, 'status_card', return_value=card) as render_card:
            text = profile_readme.portfolio(rows, 'fixture-owner')
        projection.assert_called_once_with(rows, 'fixture-owner', generated_at=None)
        render_card.assert_called_once_with(data)
        self.assertIn('```text\n' + card + '```', text)

    def test_activity_alt_uses_event_totals_and_retained_dates(self):
        data = {'activity': {'days': [
            {'date': '2026-11-19', 'opened': 1, 'merged': 2, 'closed_unmerged': 0},
            {'date': '2026-11-20', 'opened': 0, 'merged': 1, 'closed_unmerged': 1}]}}
        self.assertEqual(profile_readme.activity_alt(data),
                         'Daily activity from 2026-11-19 to 2026-11-20, Asia/Singapore dates: '
                         '1 pull requests opened, 3 merged and 1 closed without merge. Each event is dated by when it happened.')
        self.assertEqual(profile_readme.activity_alt({}), 'Daily activity: no dated events in this snapshot.')

    def test_profile_projection_matches_public_metrics_with_saved_history(self):
        from factory.core import metrics

        rows = [row(1), row(1), row(2, status='closed')]
        history = {'schema': 1, 'prs': {}}
        outcomes = {'schema': 2, 'closures': {}, 'maintainer_edits': {}, 'replacements': []}
        write_json(self.ledger_path.with_name('pr-history.json'), history)
        write_json(self.ledger_path.with_name('pr-outcomes.json'), outcomes)
        expected = metrics.compute({'prs': rows, 'pr_history': history, 'outcomes': outcomes}, CONFIG, AT)
        actual = profile_readme.portfolio_data(rows, CONFIG['github_user'], generated_at=AT)
        self.assertEqual(actual, expected)
        self.assertEqual(actual['metrics']['prs_opened']['value'], 2)

    def test_rendered_block_is_byte_identical_for_single_and_joined_aggregate_links(self):
        data = {"groups": [{"title": "Merged upstream", "repos": [
            {"repo": "alpha/core", "label": "Core", "blurb": "Shared fix"},
            {"repo": "beta/kit", "label": "Kit", "blurb": "", "join_previous": True},
            {"repo": "meta/lexical", "label": "Lexical", "blurb": "Round-trip links"}]}]}
        rows = [row(7), row(8), row(9, "beta/kit"), row(10, "beta/kit"), row(11, "facebook/lexical")]
        expected = ("**Merged upstream**\n\n"
                    "- [Core](https://github.com/alpha/core/pulls?q=is%3Apr+author%3Afixture-owner+is%3Amerged) (2)"
                    " and [Kit](https://github.com/beta/kit/pulls?q=is%3Apr+author%3Afixture-owner+is%3Amerged) (2): Shared fix\n"
                    "- [Lexical](https://github.com/meta/lexical/pull/11): Round-trip links\n")
        self.assertEqual(profile_readme.render(rows, data).encode("utf-8"), expected.encode("utf-8"))

    def test_render_count_canonical_singleton_and_missing_repo(self):
        rows = [row(), row(2), row(3, status="closed"), row(21, "beta/kit"), row(7, "facebook/lexical")]
        self.save(rows)
        self.group_file()
        text = profile_readme.render()
        self.assertIn("[Alpha](https://github.com/alpha/core/pulls?q=is%3Apr+author%3Afixture-owner+is%3Amerged) (2): keep results", text)
        self.assertIn("[Beta](https://github.com/beta/kit/pull/21): decode documents", text)
        self.assertIn("[Lexical](https://github.com/meta/lexical/pull/7): round-trip links", text)
        with self.assertRaisesRegex(ValueError, "missing/repo"):
            profile_readme.render(rows + [row(1, "missing/repo")])

    def test_snapshot_uses_fixture_blurbs_for_single_and_joined_repos(self):
        data = {"groups": [{"title": "Merged upstream", "repos": [
            {"repo": "sveltejs/kit", "label": "SvelteKit", "blurb": "Read UTF-8 assets"},
            {"repo": "meta/lexical", "label": "Lexical", "blurb": "Round-trip links"},
            {"repo": "react-hook-form/react-hook-form", "label": "React Hook Form", "blurb": "Keep field errors"},
            {"repo": "react-hook-form/resolvers", "label": "resolvers", "blurb": "", "join_previous": True}]}]}
        rows = [row(17590, "sveltejs/kit"), row(9464, "facebook/lexical")]
        rows += [row(n, "react-hook-form/react-hook-form") for n in range(4)]
        rows += [row(n, "react-hook-form/resolvers") for n in range(6)]
        text = profile_readme.render(rows, data, user="fixture-owner")
        self.assertEqual(text.splitlines(), [
            "**Merged upstream**", "",
            "- [SvelteKit](https://github.com/sveltejs/kit/pull/17590): Read UTF-8 assets",
            "- [Lexical](https://github.com/meta/lexical/pull/9464): Round-trip links",
            "- [React Hook Form](https://github.com/react-hook-form/react-hook-form/pulls?q=is%3Apr+author%3Afixture-owner+is%3Amerged) (4) and [resolvers](https://github.com/react-hook-form/resolvers/pulls?q=is%3Apr+author%3Afixture-owner+is%3Amerged) (6): Keep field errors",
        ])

    def test_joined_entry_does_not_attach_to_an_unrelated_rendered_repo(self):
        data = {"groups": [{"title": "Merged", "repos": [
            {"repo": "other/tool", "blurb": "Other fix"},
            {"repo": "alpha/core", "blurb": "Shared fix"},
            {"repo": "beta/kit", "join_previous": True, "blurb": ""}]}]}
        text = profile_readme.render([row(1, "other/tool"), row(2, "beta/kit")], data)
        self.assertNotIn(" and ", text)
        self.assertIn("- [kit](https://github.com/beta/kit/pull/2): Shared fix", text)

    def test_group_file_reports_missing_or_invalid_data(self):
        with self.assertRaisesRegex(ValueError, "restore a valid groups file"):
            profile_readme.load_groups()
        self.group_file({"groups": [{"title": "Merged", "repos": [{"repo": "broken", "blurb": "fix"}]}]})
        with self.assertRaisesRegex(ValueError, "owner/repo"):
            profile_readme.load_groups()

    def test_profile_diffs_then_writes_only_the_generated_block(self):
        self.group_file()
        self.save([row()])
        path = self.root / "profile.md"
        before = "# Profile\n\n**Building**\n\nManual \\ text.\n\n"
        after = "- Earlier: older work\n\n**Writing**: blog\n"
        original = before + "**Merged upstream**\n\n- stale\n" + after
        path.write_text(original)
        diff = self.cli("profile", "--readme", str(path))
        self.assertIn("- stale", diff)
        self.assertIn("factory:merged:start", diff)
        self.assertEqual(path.read_text(), original)
        self.cli("profile", "--readme", str(path), "--write")
        updated = path.read_text()
        self.assertTrue(updated.startswith(profile_readme.PORTFOLIO_START))
        self.assertIn(before, updated)
        self.assertTrue(updated.endswith(after))
        self.assertTrue(profile_readme.check(updated, "fixture-owner")["ok"])
        self.assertEqual(self.cli("profile", "--readme", str(path)), "")
        self.assertNotIn("records", ledger.load()[0])

    def test_profile_restores_missing_default_checkout_for_diff_and_write(self):
        self.group_file()
        self.save([row()])
        repo = f"{CONFIG['github_user']}/{CONFIG['github_user']}"
        original = "# Profile\n\nManual prose.\n"
        for explicit in (False, True):
            for write in (False, True):
                with self.subTest(explicit=explicit, write=write):
                    checkout = util.clone_dir(repo)
                    path = checkout / "README.md"

                    def restore(name):
                        checkout.mkdir(parents=True)
                        path.write_text(original)
                        return checkout

                    args = ["--readme", str(path)] if explicit else []
                    if write:
                        args.append("--write")
                    with patch.object(clones, "ensure_repo", side_effect=restore) as ensure:
                        output = self.cli("profile", *args)
                        self.assertIn(str(path) if write else "factory:merged:start", output)
                        if write:
                            self.assertTrue(path.read_text().startswith(profile_readme.PORTFOLIO_START))
                            self.assertIn(original, path.read_text())
                            self.assertTrue(profile_readme.check(path.read_text(), CONFIG["github_user"])["ok"])
                        else:
                            self.assertEqual(path.read_text(), original)
                        self.cli("profile", *args)
                        ensure.assert_called_once_with(repo)
                    self.assertNotIn("records", ledger.load()[0])
                    path.unlink()
                    checkout.rmdir()

    def test_profile_missing_custom_readme_does_not_restore(self):
        path = self.root / "custom" / "README.md"
        with self.assertRaisesRegex(SystemExit, "cannot read --readme"):
            self.cli("profile", "--readme", str(path), "--write")
        self.assertFalse(path.exists())

    def test_profile_missing_readme_in_existing_checkout_does_not_restore(self):
        checkout = util.clone_dir(f"{CONFIG['github_user']}/{CONFIG['github_user']}")
        checkout.mkdir(parents=True)
        with self.assertRaisesRegex(SystemExit, "cannot read --readme"):
            self.cli("profile", "--write")
        self.assertEqual(list(checkout.iterdir()), [])

    def test_profile_restore_failure_returns_cli_error(self):
        with patch.object(clones, "ensure_repo", side_effect=util.CommandError("origin unavailable")), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as errors:
            code = cli.main(["sync", "profile", "--write"])
        self.assertEqual(code, 3)
        self.assertIn("origin unavailable", errors.getvalue())
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(util.clone_dir(f"{CONFIG['github_user']}/{CONFIG['github_user']}").exists())

    def test_profile_outside_block_must_still_be_covered(self):
        self.group_file({"groups": [{"title": "Merged", "repos": [{"repo": "alpha/core", "blurb": "", "outside_block": True}]}]})
        self.save([row()])
        path = self.root / "profile.md"
        path.write_text("# Profile\n")
        with self.assertRaisesRegex(SystemExit, "missing_repos"):
            self.cli("profile", "--readme", str(path), "--write")
        self.assertEqual(path.read_text(), "# Profile\n")

    def test_invalid_markers_are_refused(self):
        for text in (profile_readme.START, profile_readme.END + profile_readme.START,
                     profile_readme.START * 2 + profile_readme.END):
            with self.assertRaisesRegex(ValueError, "one ordered pair"):
                profile_readme.with_markers(text)


class Interview(Workspace):
    def test_tables_use_ledger_status_item_title_and_existing_renamed_files(self):
        item = self.item("lexical", "# Keep | literal \\ values\n\nPR body")
        rows = [row(7, "facebook/lexical", item=item, merged_at="2026-11-02T03:00:00Z"),
                row(8, "meta/lexical", status="ci-failing"), row(9, "meta/lexical", status="closed")]
        directory = self.root / "docs/interview"
        folder = directory / "facebook__lexical"
        folder.mkdir(parents=True)
        (folder / "guide.md").write_text("Untouched teaching text")
        (folder / "handover.txt").write_text("Untouched handover")
        index, contributions = sync.render_interview(rows, directory)
        self.assertIn("| meta/lexical | 1 | 1 | 1 |", index)
        self.assertIn("facebook__lexical/guide.md", index)
        self.assertIn("facebook__lexical/handover.txt", index)
        self.assertNotIn("interview.md", index)
        self.assertIn("| #7 | Keep \\| literal \\\\ values | merged | 2026-11-01T12:00:00Z | 2026-11-02T03:00:00Z |", contributions)
        self.assertIn("| #8 |  | ci-failing |", contributions)
        self.assertNotIn("not the PR title", contributions)
        self.assertEqual(contributions.count("## meta/lexical"), 1)

    def test_interview_dry_run_and_idempotent_write_preserve_manual_text(self):
        self.save([row(item=self.item("alpha", "Fix results\n"))])
        directory = self.root / "docs/interview"
        directory.mkdir(parents=True)
        readme = directory / "README.md"
        before, after = "# Index\n\n", "\n\nManual historical table and \\| escapes.\n"
        readme.write_text(before + sync.INDEX_START + "\nold\n" + sync.INDEX_END + after)
        self.assertIn("Contribution records", self.cli("interview"))
        self.assertFalse((directory / "CONTRIBUTIONS.md").exists())
        self.cli("interview", "--write")
        self.assertTrue(readme.read_text().startswith(before))
        self.assertTrue(readme.read_text().endswith(after))
        self.assertEqual(self.cli("interview"), "")
        self.assertNotIn("records", ledger.load()[0])


class Notebook(Workspace):
    def test_status_and_events_are_derived_without_sending_or_marking(self):
        merged = row(1, merged_at="2026-11-09T00:00:00Z", item=self.item("merge", "# Keep results\n"))
        closed = row(2, status="closed", closed_at="2026-11-08T23:00:00Z", notes="Provider rejection")
        old = row(3, merged_at="2026-10-01T00:00:00Z", records={"notebook": RECEIPT})
        opened = row(4, status="ci-failing")
        observed = row(5, status="closed", last_check="2026-11-07T01:00:00Z")
        self.save([merged, closed, old, opened, observed])
        output = self.root / "out"
        self.cli("notebook", "--out", str(output))
        text = (output / "status.md").read_text()
        events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
        self.assertLessEqual(len(text), 6000)
        self.assertIn("alpha/core: 2 merged / 1 open / 2 closed", text)
        self.assertIn("alpha/core #4 (9d)", text)
        self.assertIn("2026-11-09 alpha/core merged: #1", text)
        self.assertNotIn("#3", text)
        self.assertEqual({e["number"] for e in events}, {1, 2, 5})
        self.assertEqual(next(e for e in events if e["number"] == 1)["title"], "Keep results")
        self.assertEqual(next(e for e in events if e["number"] == 5)["date"], observed["last_check"])
        self.assertNotIn("records", ledger.load()[0])
        self.cli("notebook", "--out", str(output))
        self.assertEqual(len((output / "events.jsonl").read_text().splitlines()), 3)

    def test_large_status_stays_bounded_and_outbox_keeps_all_owed_events(self):
        rows = [row(n, repo=f"owner/project-{n}", status="closed" if n % 2 else "submitted",
                    last_check="2026-11-09T00:00:00Z") for n in range(1000)]
        text, events = sync.notebook(rows, AT)
        self.assertLessEqual(len(text), 6000)
        self.assertIn("1000 PRs: 0 merged / 500 open / 500 closed", text)
        self.assertIn("omitted", text)
        self.assertEqual(len(events), 500)
        self.assertTrue(all(e["status"] == "closed" for e in events))

    def test_old_undated_and_unverified_merged_prs_stay_in_the_status_note(self):
        rows = [row(7, merged_at="2026-09-01T00:00:00Z"), row(8), row(9, recorded_at="old"),
                row(10, records={"notebook": {**RECEIPT, "deferred": "retry"}}),
                row(11, records={"notebook": {**RECEIPT, "sha256": ""}})]
        text, events = sync.notebook(rows, AT)
        for current in rows:
            with self.subTest(number=current["number"]):
                self.assertTrue(sync.mentioned(text, current))
        self.assertEqual(len(events), len(rows))
        self.assertLessEqual(len(text), 6000)

    def test_pending_receipts_take_priority_over_the_status_size_target(self):
        rows = [row(n, merged_at="2026-09-01T00:00:00Z") for n in range(1, 1201)]
        text, events = sync.notebook(rows, AT)
        for current in rows:
            self.assertTrue(sync.mentioned(text, current), current["number"])
        self.assertEqual(len(events), len(rows))
        self.assertNotIn("## Repositories", text)
        self.assertNotIn("## Last 14 days", text)

    def test_status_drops_oldest_recorded_merges_before_newer_ones(self):
        rows = [row(n, repo=f"owner/project-{n:03d}", merged_at=f"2026-11-{2 + n % 8:02d}T00:00:00Z",
                    records={"notebook": RECEIPT}) for n in range(150)]
        owed = row(999, "pending/older", merged_at="2026-09-01T00:00:00Z")
        text, _ = sync.notebook([*rows, owed], AT)
        self.assertLessEqual(len(text), 6000)
        self.assertIn("omitted", text)
        self.assertTrue(sync.mentioned(text, owed))
        self.assertIn("2026-11-09 owner/project-", text)
        self.assertNotIn("2026-11-02 owner/project-", text)

    def test_deferred_and_unverified_events_remain_owed(self):
        rows = [row(1, recorded_at="old"), row(2, records={"notebook": {"deferred": "later"}}),
                row(3, status="closed", records={"notebook": RECEIPT})]
        text, events = sync.notebook(rows, AT)
        self.assertEqual([e["number"] for e in events], [1, 2])
        self.assertTrue(all(e["date"] is None for e in events))
        self.assertNotIn("1970", text)


class Audit(Workspace):
    def test_audit_checks_only_unverified_and_skips_unsupplied_destinations(self):
        rows = [row(7, "facebook/lexical", recorded_at="old"), row(8, recorded_at="old"),
                row(9), row(10, records={"profile": {"deferred": "later"}})]
        self.save(rows)
        path = self.root / "profile.md"
        content = "https://github.com/meta/lexical/pull/7\nalpha/core #9\nalpha/core #10\n"
        path.write_text(content)
        result = json.loads(self.cli("audit", "--readme", str(path)))
        self.assertEqual(result["skipped"], ["teach", "notebook"])
        self.assertEqual(len(result["changed"]), 1)
        marked = ledger.find(ledger.load(), rows[0]["url"])
        self.assertEqual(marked["records"]["profile"]["ref"], "audit")
        self.assertEqual(marked["records"]["profile"]["sha256"], hashlib.sha256(content.encode()).hexdigest())
        self.assertEqual(sync.status()["profile"][-1], rows[1]["url"])
        self.assertEqual(json.loads(self.cli("audit", "--readme", str(path)))["changed"], [])

    def test_profile_audit_uses_the_same_aggregate_receipt_rule(self):
        rows = [row(7, recorded_at="old"), row(8, recorded_at="old")]
        self.save(rows)
        path = self.root / "profile.md"
        path.write_text(profile_readme.render(rows, groups()))
        result = json.loads(self.cli("audit", "--readme", str(path)))
        self.assertEqual(result["missing"], [])
        self.assertEqual([r["url"] for r in result["changed"]], [r["url"] for r in rows])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        for saved in ledger.load():
            self.assertEqual(saved["records"]["profile"]["sha256"], digest)
            self.assertTrue(sync.real_receipt(saved["records"]["profile"]))
            self.assertTrue(sync.unverified(saved["records"]["teach"]))

    def test_profile_audit_deduplicates_alias_pr_numbers_without_losing_rows(self):
        rows = [row(7, "facebook/lexical", recorded_at="old"), row(8, "meta/lexical", recorded_at="old"),
                row(7, "meta/lexical", recorded_at="old")]
        self.save(rows)
        path = self.root / "profile.md"
        path.write_text(profile_readme.render(rows, groups()))
        result = json.loads(self.cli("audit", "--readme", str(path)))
        self.assertEqual(result["missing"], [])
        self.assertEqual({r["url"] for r in result["changed"]}, {r["url"] for r in rows})
        for saved in ledger.load():
            self.assertEqual(saved["records"]["profile"]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertTrue(sync.real_receipt(saved["records"]["profile"]))
            self.assertTrue(sync.unverified(saved["records"]["teach"]))

    def test_audit_reports_missing_receipts_without_clearing_by_default(self):
        current = row(recorded_at="old", notes="keep")
        self.save([current])
        path = self.root / "profile.md"
        path.write_text("No PR recorded here")
        before = self.ledger_path.read_bytes()
        result = json.loads(self.cli("audit", "--readme", str(path)))
        self.assertEqual(result["missing"], [{"destination": "profile", "url": current["url"]}])
        self.assertEqual(result["cleared"], [])
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_clear_missing_materializes_legacy_receipts_only_for_supplied_sources(self):
        current = row(recorded_at="old", notes="keep", adopted_in="upstream")
        self.save([current])
        path = self.root / "profile.md"
        path.write_text("No PR recorded here")
        result = json.loads(self.cli("audit", "--readme", str(path), "--clear-missing"))
        self.assertEqual(result["cleared"], [{"destination": "profile", "url": current["url"]}])
        saved = ledger.load()[0]
        self.assertEqual(saved["records"], {d: {"at": "old", "unverified": True} for d in ("teach", "notebook")})
        self.assertEqual(saved["notes"], "keep")
        self.assertEqual(saved["adopted_in"], "upstream")
        self.assertEqual(saved["recorded_at"], "old")
        self.assertIn(current["url"], sync.status()["profile"])
        before = self.ledger_path.read_bytes()
        second = json.loads(self.cli("audit", "--readme", str(path), "--clear-missing"))
        self.assertEqual(second["cleared"], [])
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_clear_missing_preserves_real_deferred_and_present_records(self):
        rows = [row(1, recorded_at="old"), row(2, records={"profile": RECEIPT}),
                row(3, records={"profile": {"deferred": "later"}}),
                row(4, records={"profile": {"unverified": True, "at": "old"}, "teach": RECEIPT})]
        self.save(rows)
        profile = self.root / "profile.md"
        profile.write_text("alpha/core #1")
        result = json.loads(self.cli("audit", "--readme", str(profile), "--clear-missing"))
        self.assertEqual(result["changed"], [{"destination": "profile", "url": rows[0]["url"], "ref": "audit"}])
        self.assertEqual(result["cleared"], [{"destination": "profile", "url": rows[3]["url"]}])
        saved = ledger.load()
        self.assertTrue(sync.real_receipt(saved[0]["records"]["profile"]))
        self.assertEqual(saved[1:3], rows[1:3])
        self.assertEqual(saved[3]["records"], {"teach": RECEIPT})

    def test_clear_missing_for_all_destinations_does_not_revive_legacy_receipts(self):
        self.save([row(recorded_at="old")])
        profile = self.root / "profile.md"
        profile.write_text("No PR here")
        teaching = self.root / "teaching"
        teaching.mkdir()
        result = json.loads(self.cli("audit", "--clear-missing", "--readme", str(profile),
                                     "--interview-dir", str(teaching), "--notebook-file", str(profile)))
        self.assertEqual(len(result["cleared"]), 3)
        self.assertEqual(ledger.load()[0]["records"], {})
        self.assertEqual(sync.records(ledger.load()[0]), {})
        self.assertEqual(json.loads(self.cli("audit", "--clear-missing"))["skipped"], list(sync.DESTINATIONS))

    def test_clear_missing_requires_all_supplied_text_to_be_readable_before_writing(self):
        self.save([row(recorded_at="old")])
        profile = self.root / "profile.md"
        profile.write_text("No PR here")
        notebook = self.root / "status.md"
        notebook.write_bytes(b"\xff")
        before = self.ledger_path.read_bytes()
        with self.assertRaises(SystemExit):
            self.cli("audit", "--clear-missing", "--readme", str(profile), "--notebook-file", str(notebook))
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_clear_missing_rereads_under_the_receipt_lock(self):
        current = row(recorded_at="old")
        self.save([current])
        profile = self.root / "profile.md"
        profile.write_text("No PR here")
        destination = self.root / "destination.md"
        destination.write_text(current["url"])
        audit_receipt = sync.audit_receipt

        def concurrent_mark(dest, url, digest):
            sync.mark("profile", url, file=destination, ref="new-revision")
            sync.mark("teach", url, file=destination, ref="other-destination")
            return audit_receipt(dest, url, digest)

        with patch.object(sync, "audit_receipt", side_effect=concurrent_mark):
            result = sync.audit(readme=profile, clear_missing=True)
        self.assertEqual(result["cleared"], [])
        saved = ledger.load()[0]["records"]
        self.assertEqual(saved["profile"]["ref"], "new-revision")
        self.assertEqual(saved["teach"]["ref"], "other-destination")

    def test_audit_matches_repo_number_pairs_without_substring_collisions(self):
        r = row(7)
        for content in ("alpha/core #7", "| alpha/core | #7 |", "https://github.com/alpha/core/pull/7"):
            self.assertTrue(sync.mentioned(content, r), content)
        for content in ("alpha/core #70", "alpha/core #7oops", "other/repo #7", "xalpha/core #7", "alpha/core-tools #7",
                        "alpha/core #8; beta/kit #7", "https://github.com/alpha/core/pull/70"):
            self.assertFalse(sync.mentioned(content, r), content)

    def test_audit_reads_directory_and_notebook_text_and_retains_missing_records(self):
        self.save([row(1, recorded_at="old"), row(2, recorded_at="old")])
        folder = self.root / "interview"
        folder.mkdir()
        (folder / "guide.md").write_text("alpha/core #1")
        life = self.root / "status.md"
        life.write_text("alpha/core #2")
        result = json.loads(self.cli("audit", "--interview-dir", str(folder), "--notebook-file", str(life)))
        self.assertEqual({(c["destination"], c["url"]) for c in result["changed"]},
                         {("teach", row(1)["url"]), ("notebook", row(2)["url"])})
        self.assertEqual(sync.status()["teach"], [row(2)["url"]])
        self.assertEqual(sync.status()["notebook"], [row(1)["url"]])


class Retrospective(Workspace):
    def pr(self, number, created, merged=None, state="OPEN"):
        return {"url": f"https://github.com/alpha/core/pull/{number}", "number": number, "title": "Keep results",
                "createdAt": created, "mergedAt": merged, "closedAt": merged, "state": "MERGED" if merged else state,
                "additions": 4, "deletions": 2, "repository": {"nameWithOwner": "alpha/core", "stargazerCount": 20000}}

    def test_populations_include_old_submissions_merged_in_window(self):
        prs = [self.pr(1, "2026-10-01T00:00:00Z", "2026-11-01T17:00:00Z"),
               self.pr(2, "2026-11-01T16:00:00Z", "2026-11-02T15:00:00Z"),
               self.pr(3, "2026-11-02T01:00:00Z"),
               self.pr(4, "2026-10-01T00:00:00Z", "2026-11-01T15:59:59Z")]
        report = retro.populations(prs, "2026-11-02", AT)
        submitted, merged = (report["populations"][name] for name in ("submitted since", "merged since"))
        self.assertEqual((submitted["total"], submitted["merged"], submitted["open"]), (2, 1, 1))
        self.assertEqual(merged["total"], 2)
        self.assertEqual(report["since_utc"], "2026-11-01T16:00:00+00:00")
        self.assertEqual(merged["merged_per_day"], {"2026-11-02": 2})
        self.assertEqual(merged["utc"]["merged_per_day"], {"2026-11-01": 1, "2026-11-02": 1})
        self.assertEqual(merged["utc"]["day_timezone"], "UTC")
        self.assertEqual(merged["day_timezone"], "UTC+8")
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            retro.populations(prs, "yesterday", AT)

    def test_retro_fetches_both_populations_deduplicates_and_writes_source(self):
        p1 = self.pr(1, "2026-11-02T00:00:00Z", "2026-11-02T10:00:00Z")
        p2 = self.pr(2, "2026-10-01T00:00:00Z", "2026-11-02T12:00:00Z")
        out = self.root / "retro"
        with patch.object(retro, "fetch", side_effect=[[p1], [p1, p2]]) as fetch:
            result = json.loads(self.cli("retro", "--since", "2026-11-02", "--write", str(out)))
        self.assertEqual(result["populations"]["merged since"]["total"], 2)
        self.assertEqual(len(json.loads((out / "prs.json").read_text())), 2)
        self.assertEqual(json.loads((out / "stats.json").read_text()), result)
        self.assertEqual([call.kwargs["population"] for call in fetch.call_args_list], ["submitted", "merged"])
        self.assertEqual(fetch.call_args_list[0].args[1], "2026-11-01")

    def test_fetch_uses_merged_search_and_refuses_truncated_totals(self):
        data = {"data": {"search": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}
        with patch.object(retro, "gh_json", return_value=data) as gh:
            retro.fetch("fixture-owner", "2026-11-01", population="merged")
        self.assertIn("merged:>=2026-11-01", " ".join(gh.call_args.args))
        data["data"]["search"]["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
        with patch.object(retro, "gh_json", return_value=data), self.assertRaisesRegex(ValueError, "narrow --since"):
            retro.fetch("fixture-owner", "2026-11-01", pages=1)


if __name__ == "__main__":
    unittest.main()
