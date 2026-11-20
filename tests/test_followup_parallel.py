"""Repository-scoped parallel reads preserve events, cursors and bounded concurrency."""
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import setup_module
from factory import util
from factory.core import ledger
from factory.oss import followup
from factory.util import CONFIG, iso, write_json


def setUpModule():
    setup_module(unittest)


class ParallelObservations(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.path = self.root / "prs.json"
        self.rows = [{"repo": f"example/repo-{n % 8}", "number": n + 1,
                      "url": f"https://github.com/example/repo-{n % 8}/pull/{n + 1}",
                      "status": "submitted", "submitted_at": iso()} for n in range(22)]
        write_json(self.path, self.rows)
        self.active, self.maximum, self.by_repo = 0, 0, set()
        self.mergers, self.fail = [], None
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(4)
        self.entered = set()
        for mocker in (patch.dict(CONFIG, ledger_file=str(self.path), lessons_file=str(self.root / "lessons.md")),
                       patch.object(util, "global_hooks_drift", return_value=[]),
                       patch.object(util, "local_hooks_overrides", return_value=[]),
                       patch.object(followup, "gh_json", side_effect=self.lookup),
                       patch.object(followup, "gh_paginate", return_value=[]),
                       patch.object(followup.pr_signals, "lines", return_value=[])):
            mocker.start()
            self.addCleanup(mocker.stop)

    def lookup(self, *args, **kwargs):
        if args[:2] == ("api", "graphql"):
            self.mergers.append(next(a for a in args if a.startswith("name=")))
            return {"data": {"repository": {"pullRequests": {"nodes": []}}}}
        self.assertEqual(args[:2], ("pr", "view"))
        url = args[2]
        repo = url.split("/pull/")[0]
        with self.lock:
            self.assertNotIn(repo, self.by_repo)
            self.by_repo.add(repo)
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            first = threading.get_ident() not in self.entered
            self.entered.add(threading.get_ident())
        try:
            if first:
                self.barrier.wait(timeout=5)
            if url == self.fail:
                raise ValueError("fixture observation failed")
            return {"state": "OPEN", "mergedAt": None, "reviewDecision": "REVIEW_REQUIRED",
                    "headRefOid": "a" * 40, "statusCheckRollup": [], "commits": []}
        finally:
            with self.lock:
                self.by_repo.remove(repo)
                self.active -= 1

    def test_twenty_two_prs_are_bounded_and_persisted_before_cursors(self):
        merge = ledger.merge
        def persist(checked):
            events = [json.loads(line) for line in self.path.with_name("followup-events.jsonl").read_text().splitlines()]
            self.assertEqual({e["url"] for e in events if e["kind"] == "feedback-read"}, {r["url"] for r in checked})
            merge(checked)
        with patch.object(ledger, "merge", side_effect=persist):
            followup.run()
        self.assertEqual(self.maximum, 4)
        self.assertEqual(len(self.mergers), 8)
        rows = ledger.load()
        self.assertEqual(len(rows), 22)
        self.assertTrue(all(r.get("last_check") and r.get("feedback_since") for r in rows))
        events = followup.events()
        self.assertEqual(len({e["key"] for e in events}), len(events))
        self.assertEqual(sum(e["kind"] == "feedback-read" for e in events), 22)
        self.assertIsNone(followup._EVENT_SINK.get())

    def test_failed_pr_keeps_its_cursor_and_does_not_lose_other_repositories(self):
        self.fail = self.rows[3]["url"]
        messages = followup.run()
        self.assertTrue(any("fixture observation failed" in message for message in messages))
        rows = ledger.load()
        self.assertNotIn("last_check", ledger.find(rows, self.fail))
        self.assertEqual(sum(bool(r.get("last_check")) for r in rows), 21)
        self.assertTrue(any(e["kind"] == "followup-error" and e["url"] == self.fail for e in followup.events()))

    def test_serial_and_parallel_runs_have_the_same_action_order(self):
        self.barrier = threading.Barrier(1)
        with patch.object(followup, "OBSERVATION_WORKERS", 1):
            serial = followup.run()
        write_json(self.path, self.rows)
        self.path.with_name("followup-events.jsonl").unlink()
        parallel = followup.run()
        self.assertEqual(parallel, serial)
