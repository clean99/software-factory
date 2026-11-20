"""Failed merger reads must neither lose feedback nor block unrelated work."""
import contextlib
import copy
import datetime as dt
import itertools
import json
import unittest
from unittest.mock import patch

from support import setup_module
import test_next as fixtures
from factory.core import ledger, nextaction
from factory.oss import followup
from factory.util import write_json

SUCCESS = {"data": {"repository": {"pullRequests": {"nodes": [{"mergedBy": {"login": "private-maintainer"}}]}}}}


def setUpModule():
    setup_module(unittest)


class Recovery(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.NextActions()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.fail = True
        self.pr = {"state": "OPEN", "mergedAt": None, "reviewDecision": "", "statusCheckRollup": [], "commits": [],
                   "mergeable": "MERGEABLE", "baseRefName": "main", "headRefOid": fixtures.HEAD}
        self.comment = {"id": 17, "author": {"login": "private-maintainer"}, "authorAssociation": "CONTRIBUTOR",
                        "createdAt": (self.f.clock - dt.timedelta(minutes=1)).isoformat(), "body": "Please add a boundary test."}

    @contextlib.contextmanager
    def producer(self):
        def gh(*args, **kwargs):
            if args[:2] == ("pr", "view"):
                return copy.deepcopy(self.pr)
            self.assertEqual(args[:2], ("api", "graphql"))
            if self.fail:
                raise RuntimeError("merger-read outage")
            return copy.deepcopy(SUCCESS)

        def responses(repo, number, reviews=False):
            if number != 1:
                return []
            if reviews:
                return [{**self.comment, "id": 18, "submittedAt": self.comment["createdAt"], "state": "COMMENTED"}]
            return [copy.deepcopy(self.comment)]

        with patch.object(followup, "_MERGERS", {}), patch.object(followup, "_EVENTS", []), \
                patch.object(followup, "now", side_effect=lambda: self.f.clock), \
                patch.object(followup, "iso", side_effect=lambda *args: self.f.clock.isoformat()), \
                patch.object(followup, "gh_json", side_effect=gh), \
                patch.object(followup, "fetch_responses", side_effect=responses), \
                patch.object(followup.pr_signals, "lines", return_value=[]):
            yield

    def scan(self, url):
        row = copy.deepcopy(next(r for r in ledger.load() if r["url"] == url))
        row.setdefault("rev", 0)
        followup._EVENTS.clear()
        followup.check_pr(row, "fixture-owner", [])
        followup.persist_events()
        ledger.merge([row])
        return next(r for r in ledger.load() if r["url"] == url)

    def feedback(self):
        return [e for e in nextaction.observations(False)[0] if e["kind"] in nextaction.FEEDBACK]

    def test_historical_cursor_and_metadata_free_feedback_are_recovered(self):
        f = self.f
        item = f.item("historical", status="submitted", pr_url="https://github.com/o/r/pull/1")
        row = f.row(item=str(item.dir.relative_to(f.root)))
        with self.producer():
            row = self.scan(row["url"])
            row.pop("feedback_since")
            write_json(f.ledger, [row])
            path = f.ledger.with_name("followup-events.jsonl")
            events = [json.loads(line) for line in path.read_text().splitlines()]
            for e in events:
                for key in ("author_login", "operation", "merger_error", "consumer"):
                    e["detail"].pop(key, None)
            path.write_text("".join(json.dumps(e) + "\n" for e in events if e["kind"] != "feedback-read"))
            f.clock += dt.timedelta(minutes=1)
            f.event("repo-mergers-read", {"url": "https://github.com/o/r", "repo": "o/r"}, {})
            self.assertEqual(len(f.actions("repair")), 1)
            history = path.read_bytes()
            f.clock += dt.timedelta(minutes=1)
            self.fail = False
            self.scan(row["url"])
            self.assertEqual({e["kind"] for e in self.feedback()}, {"maintainer-comment", "maintainer-review"})
            self.assertEqual(f.actions("repair"), [])
            self.assertEqual(f.wave()["tasks"][0]["role"], "follow")
            self.assertTrue(path.read_bytes().startswith(history))

    def test_handled_projection_keeps_its_identity_and_time_on_canonical_reread(self):
        f = self.f
        item = f.item("handled", status="submitted", pr_url="https://github.com/o/r/pull/1")
        a = f.row(item=str(item.dir.relative_to(f.root)))
        b = f.row(number=2)
        with self.producer():
            first = self.scan(a["url"])
            self.assertEqual(first["feedback_since"], a["submitted_at"])
            self.assertEqual(first["last_check"], f.clock.isoformat())
            f.clock += dt.timedelta(minutes=1)
            self.fail = False
            self.scan(b["url"])
            projected = self.feedback()
            self.assertEqual(len(projected), 2)
            self.assertEqual(f.actions("repair"), [])
            item.file("reply.md").write_text("Added the requested test.")
            write_json(item.file("follow.json"), {"event_keys": [e["key"] for e in projected], "reply_file": "reply.md", "reply_only": True,
                                                "requests": [{"request": "boundary", "disposition": "accepted", "evidence": "receipt"}]})
            f.review(item)
            f.review(item, coordinator=True)
            item.meta["replies"] = [{"file": "reply.md", "url": a["url"] + "#issuecomment-1", "at": f.clock.isoformat()}]
            item.save()
            self.assertTrue(nextaction.follow_replied(item, projected))
            f.clock += dt.timedelta(minutes=1)
            self.scan(a["url"])
            reread = self.feedback()
            self.assertEqual([(e["key"], e["at"]) for e in projected], [(e["key"], e["at"]) for e in reread])
            self.assertTrue(nextaction.follow_replied(item, reread))
            self.assertEqual(f.actions("ship"), [])
            self.assertIsNone(f.wave())
            with patch.object(followup, "events", return_value=followup._EVENTS):
                self.assertEqual(nextaction.observations(True)[0], nextaction.observations(False)[0])

    def test_merger_failure_does_not_freeze_ci_or_pr_recovery(self):
        f = self.f
        row = f.row(last_check=(f.clock - dt.timedelta(minutes=1)).isoformat(), status="ci-failing", checks={"failing": ["unit"]})
        f.event("ci-failing", row, {"head_sha": fixtures.HEAD, "jobs": [{"name": "unit", "head_sha": fixtures.HEAD}]})
        error = f.event("followup-error", row, {"operation": "review-threads", "text": "previous thread outage"})
        f.clock += dt.timedelta(minutes=1)
        self.pr["statusCheckRollup"] = [{"name": "unit", "status": "COMPLETED", "conclusion": "SUCCESS", "databaseId": 6}]
        with self.producer():
            row = self.scan(row["url"])
        self.assertEqual(f.actions("ci-triage"), [])
        self.assertFalse(nextaction.current_event(error, {row["url"]: row}))
        self.assertEqual([a["target"] for a in f.actions("repair")], ["https://github.com/o/r"])

    def test_overlapping_success_waits_for_the_failed_producer(self):
        f = self.f
        row = f.row()
        self.pr["statusCheckRollup"] = [{"name": "unit", "status": "COMPLETED", "conclusion": "FAILURE", "databaseId": 5}]

        def base_read(*args, **kwargs):
            followup.persist_events()
            f.clock += dt.timedelta(minutes=1)
            f.read_mergers(copy.deepcopy(SUCCESS))
            # The read is successful, but the producer has not emitted feedback yet.
            self.assertEqual(len(f.actions("repair")), 1)
            f.clock += dt.timedelta(minutes=1)
            return followup.BaseFailures({"unit"})

        with self.producer(), patch.object(followup, "failing_on_base", side_effect=base_read):
            self.scan(row["url"])
        self.assertEqual({e["kind"] for e in self.feedback()}, {"maintainer-comment", "maintainer-review"})
        self.assertEqual(f.actions("repair"), [])

    def test_interrupted_producer_recovers_on_a_complete_replay(self):
        f = self.f
        row = f.row()
        with self.producer(), patch.object(followup, "failing_on_base", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                followup.check_pr(row, "fixture-owner", [])
            followup.persist_events()
        f.clock += dt.timedelta(minutes=1)
        self.fail = False
        with self.producer():
            self.scan(row["url"])
        self.assertEqual(f.actions("repair"), [])
        self.assertEqual(len(self.feedback()), 2)

    def test_live_repeated_pr_error_is_not_hidden_by_its_older_saved_key(self):
        f = self.f
        row = f.row()
        error = f.event("followup-error", row)
        f.clock += dt.timedelta(minutes=1)
        row["last_check"] = f.clock.isoformat()
        write_json(f.ledger, [row])
        f.clock += dt.timedelta(minutes=1)
        repeated = {**error, "at": f.clock.isoformat()}
        with patch.object(followup, "events", return_value=[repeated]):
            projected, _ = nextaction.observations(True)
        self.assertEqual(projected, [repeated])
        self.assertTrue(nextaction.current_event(projected[0], {row["url"]: row}))

    def test_interrupted_read_recovers_after_another_failed_scan_marks_pr_merged(self):
        f = self.f
        row = f.row()
        with self.producer(), patch.object(followup, "failing_on_base", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                followup.check_pr(row, "fixture-owner", [])
            followup.persist_events()
        f.clock += dt.timedelta(minutes=1)
        self.pr.update(state="MERGED", mergedAt=f.clock.isoformat())
        with self.producer():
            self.scan(row["url"])
        self.assertEqual(len(f.actions("repair")), 2)
        f.clock += dt.timedelta(minutes=1)
        f.read_mergers(copy.deepcopy(SUCCESS))
        self.assertEqual(f.actions("repair"), [])
        f.lanes = ["o/r"]
        f.plan["repo_slots"] = [{"repo": "o/r", "free": 1, "merged": 1}]
        self.assertEqual(f.wave()["tasks"][0]["repo"], "o/r")

    def test_repository_operation_order_and_timestamp_boundaries(self):
        f = self.f
        path = f.ledger.with_name("followup-events.jsonl")
        for sequence in itertools.product(("fail-a", "read-a", "fail-b", "read-b", "pr-read", "other-error"), repeat=4):
            events, failures, last_success = [], {}, {}
            for index, token in enumerate(sequence):
                repo = "o/b" if token.endswith("-b") else "o/a"
                kind = "followup-error" if token.startswith("fail") or token == "other-error" else "repo-mergers-read"
                operation = "different-operation" if token == "other-error" else "repo-mergers"
                url = "https://github.com/" + repo
                if token == "pr-read":
                    url += "/pull/1"
                    kind = "maintainer-comment"
                events.append({"key": str(index), "kind": kind, "url": url, "detail": {"operation": operation},
                               "at": (f.clock + dt.timedelta(seconds=index)).isoformat()})
                if kind == "followup-error":
                    failures[str(index)] = (repo, operation, index)
                elif kind == "repo-mergers-read":
                    last_success[repo] = index
            expected = {key for key, (repo, operation, index) in failures.items()
                        if operation != "repo-mergers" or last_success.get(repo, -1) <= index}
            path.write_text("".join(json.dumps(e) + "\n" for e in reversed(events)))
            self.assertEqual({e["key"] for e in nextaction.observations(False)[0] if e["kind"] == "followup-error"}, expected, sequence)
        path.unlink()
        f.read_mergers(copy.deepcopy(SUCCESS))
        f.read_mergers(RuntimeError("same-time failure"))
        self.assertEqual(len(f.actions("repair")), 1)
        f.read_mergers(RuntimeError("same-time failure"))
        self.assertEqual(len(f.actions("repair")), 2)
