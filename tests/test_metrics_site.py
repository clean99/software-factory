"""Pure formatting, projection, escaping and asset-integrity contracts."""
import base64
import copy
import datetime as dt
import hashlib
import json
import pathlib
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from html import unescape
from html.parser import HTMLParser
from unittest.mock import patch

from support import setup_module
from factory.core import metrics, metrics_site as site


CONFIG = {"github_user": "example", "public": {"repository": "example/factory",
          "dashboard_url": "https://example.github.io/factory/"}}
NOW = dt.datetime(2026, 11, 21, tzinfo=dt.timezone.utc)


def setUpModule():
    setup_module(unittest)


def projection():
    rows = [{"url": f"https://github.com/example/project/pull/{number}", "status": "merged",
             "submitted_at": "2026-10-24T00:00:00Z", "merged_at": "2026-11-01T00:00:00Z"}
            for number in range(1, 25)]
    return metrics.compute({"prs": rows}, CONFIG, NOW)


class Fragment(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tags, self.text = [], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, text):
        self.text.append(text)


def words(text):
    return " ".join(" ".join(Fragment(text).text).split())


class SharedAssetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.assets = pathlib.Path(temporary.name)
        (self.assets / "fonts").mkdir()
        (self.assets / "site.css").write_text(
            '@font-face{font-family:Display;font-weight:600;src:url(data:font/woff2;base64,@@FONT_600@@)}\n'
            '@font-face{font-family:Display;font-weight:800;src:url(data:font/woff2;base64,@@FONT_800@@)}\n'
            'body{font-family:Display,sans-serif}\n')
        (self.assets / "site.js").write_text('\n  "use strict";\nconst label = "test";\n\n')
        assets = patch.object(site, "ASSETS", self.assets)
        assets.start()
        self.addCleanup(assets.stop)

    def test_policy_hashes_exactly_the_returned_script(self):
        values = site.shared()
        digest = base64.b64encode(hashlib.sha256(values["SCRIPT"].encode()).digest()).decode()
        self.assertIn("script-src 'sha256-" + digest + "'", values["CSP"])
        self.assertNotIn("script-src 'unsafe-inline'", values["CSP"])
        self.assertEqual(values["SCRIPT"], (self.assets / "site.js").read_text().strip())
        self.assertEqual(values, site.shared())

    def test_missing_fonts_leave_the_system_fallback_without_slots(self):
        css = site.shared()["BASE_CSS"]
        self.assertNotIn("@font-face", css)
        self.assertNotIn("@@", css)
        self.assertIn("sans-serif", css)

    def test_malformed_font_drops_only_its_face(self):
        valid = pathlib.Path(site.__file__).parent / "fonts/bigshoulders-800.woff2.b64"
        (self.assets / "fonts/bigshoulders-800.woff2.b64").write_bytes(valid.read_bytes())
        for bad in (b"not base64!", b"abcde", "invalid é".encode()):
            with self.subTest(bad=bad):
                (self.assets / "fonts/bigshoulders-600.woff2.b64").write_bytes(bad)
                css = site.shared()["BASE_CSS"]
                self.assertFalse("font-weight:600" in css, "malformed 600 font must drop its face")
                self.assertIn("font-weight:800", css)
                self.assertNotIn("@@", css)

    def test_fill_refuses_an_unprovided_slot(self):
        (self.assets / "template.html").write_text("@@KNOWN@@ / @@UNPROVIDED@@")
        with self.assertRaisesRegex(KeyError, "UNPROVIDED"):
            site.fill("template.html", {"KNOWN": "present"})


class DailyFormatTests(unittest.TestCase):
    def test_daily_falls_back_to_ledger_counts_and_filters_non_rows(self):
        data = projection()
        expected = {key: data["metrics"]["prs_" + key]["value"] for key in ("opened", "merged", "closed", "open")}
        for missing in (None, [], "invalid"):
            with self.subTest(activity=missing):
                data["activity"] = missing
                self.assertEqual(site.daily(data), ({}, expected, []))
        data["activity"] = {"totals": {"opened": 7, "merged": None}, "days": [None, {}, "bad"]}
        _, counts, days = site.daily(data)
        self.assertEqual(counts, {**expected, "opened": 7, "merged": None})
        self.assertEqual(days, [{}])
        data["activity"] = {"totals": "invalid"}
        self.assertEqual(site.daily(data)[1:], (expected, []))

    def test_strip_uses_one_scale_marks_zero_and_last_day_and_is_stable(self):
        days = [{"date": "2026-11-20", "opened": 8, "merged": 4, "closed_unmerged": 2},
                {"date": "2026-11-21", "opened": 0, "merged": 2, "closed_unmerged": 0}]
        original = copy.deepcopy(days)
        text = site.strip(days)
        marks = [attrs for tag, attrs in Fragment(text).tags if tag == "i"]
        heights = [int(re.search(r"--h:(\d+)", attrs["style"])[1]) for attrs in marks]
        self.assertEqual(heights, [100, 0, 50, 25, 25, 0])
        for index, mark in enumerate(marks):
            self.assertEqual("z" in mark.get("class", "").split(), heights[index] == 0)
            self.assertEqual("today" in mark.get("class", "").split(), index % 2 == 1)
        self.assertEqual(text, site.strip(days))
        self.assertEqual(days, original)
        self.assertIn("No dated events", words(site.strip([])))

    def test_today_line_checked_failed_and_never_checked(self):
        data = projection()
        activity = data["activity"]
        for observation, expected in (
                ({"ok": True, "checked_at": "2026-11-20T17:01:00Z"},
                 "Checked against GitHub at 2026-11-21 01:01 (Asia/Singapore)."),
                ({"ok": False, "checked_at": "2026-11-20T17:01:00Z"},
                 "GitHub could not be reached for this snapshot. Counts are as of 2026-11-21 01:01 (Asia/Singapore)."),
                ({}, "This snapshot was not checked against GitHub; counts come from the factory's ledger.")):
            with self.subTest(observation=observation):
                activity["observation"] = observation
                self.assertIn(expected, words(site.today_line(data)))
        activity["missing"] = {"opened_at": 1, "merged_at": 2, "closed_at": 3}
        activity["replacements"] = {"pairs": 2}
        text = words(site.today_line(data))
        for count, event in ((1, "opening"), (2, "merge"), (3, "close")):
            noun = "pull request" + ("s" if count != 1 else "")
            self.assertIn(f"{count} {noun} with no recorded {event} time: counted in the totals, left out of the days.", text)
        self.assertIn("2 replacement pairs counted once.", text)
        self.assertIn("Since 2026-10-24:", text)

    def test_rate_block_keeps_counts_below_the_minimum_sample(self):
        row = {"value": None, "numerator": 18, "denominator": 19}
        text = words(site.rate_block(row, "", "Merge rate", "counts", "Maintainers decide"))
        self.assertIn("18 of 19", text)
        self.assertIn("too few to state a rate", text)
        self.assertNotIn("%", text)

    def test_proof_and_funnel_do_not_turn_missing_values_into_zero(self):
        self.assertEqual(words(site.proof(None, "receipts")), "Not recorded in this snapshot.")
        self.assertIn("0", words(site.proof(0, "receipts")))
        values = {key: {"value": None} for key in
                  ("candidates_found", "candidates_stopped_before_publish", "sent_back_review")}
        self.assertEqual(site.funnel(values, None), "")
        values["sent_back_review"]["value"] = 0
        self.assertIn("0", words(site.funnel(values, None)))
        self.assertNotIn("None", site.funnel(values, None))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.assets = pathlib.Path(temporary.name)
        self.file = self.assets / "public-evidence.json"
        self.selection = {"merged": [{"repo": "Example/Project", "number": n, "label": f"Case {n}",
                                      "text": "A repair", "basis": "A regression test"} for n in (1, 2)],
                          "closure": {"repo": "example/project", "number": 3, "date": "2026-11-20",
                                      "quote": "Not wanted", "quoted": "Maintainer", "happened": "Closed",
                                      "changed": "Selection changed"}}
        self.file.write_text(json.dumps(self.selection))
        assets = patch.object(site, "ASSETS", self.assets)
        assets.start()
        self.addCleanup(assets.stop)

    def pulls(self, *buckets):
        return [{"repository": "example/project", "number": n, "bucket": bucket}
                for n, bucket in enumerate(buckets, 1)]

    def test_selection_is_bound_to_outcomes_and_titles_count_shown_cards(self):
        for bucket in ("merged_reworked", "merged_unverified", "open", "closed", None):
            with self.subTest(bucket=bucket):
                title, cards = site.evidence_cards(self.pulls(bucket, "merged_no_rework", "open"))
                self.assertEqual(title, "One merge from the ledger")
                self.assertNotIn("Case 1", words(cards))
                self.assertIn("Case 2", words(cards))
                self.assertNotIn("Not wanted", words(cards))
        title, cards = site.evidence_cards(self.pulls("merged_no_rework", "merged_no_rework", "closed"))
        self.assertEqual(title, "Two merges and the closure that changed the rules")
        self.assertIn("Not wanted", words(cards))
        title, _ = site.evidence_cards(self.pulls("open", "open", "closed"))
        self.assertEqual(title, "The closure that changed the rules")
        self.assertEqual(site.evidence_cards([])[0], "Selected pull requests")

    def test_malformed_or_missing_selection_degrades_to_a_note(self):
        for text in ("{", "null", "[]", '{"merged": [null]}', '{"merged": "bad"}'):
            with self.subTest(text=text):
                self.file.write_text(text)
                title, cards = site.evidence_cards(self.pulls("merged_no_rework"))
                self.assertEqual(title, "Selected pull requests")
                self.assertEqual(words(cards), "No selection is recorded in this snapshot.")
        self.file.unlink()
        self.assertIn("No selection", words(site.evidence_cards([])[1]))


class StatusCardTests(unittest.TestCase):
    def fixture(self):
        data = projection()
        data["activity"]["totals"].update(opened=129, merged=101, closed_unmerged=10, open=18)
        data["population"]["merged_projects"] = 19
        data["metrics"]["cohort_merge_rate"].update(numerator=96, denominator=124, value=96 / 124)
        data["metrics"]["merged_no_rework_share"].update(numerator=68, denominator=96, value=68 / 96)
        for day in data["activity"]["days"]:
            day.update(opened=0, merged=0, closed_unmerged=0)
        data["activity"]["days"][-2]["merged"] = 3
        data["activity"]["days"][-1]["merged"] = 1
        data["activity"]["observation"] = {"ok": True, "checked_at": NOW.isoformat()}
        return data

    def test_status_card_matches_the_public_text_contract(self):
        expected = ("software-factory\nsnapshot 2026-11-21, Asia/Singapore date\n\n"
                    "opened   129  since 2026-10-24\nmerged   101  in 19 projects\n"
                    "closed    10  without merge\nopen      18\n\n"
                    "merge rate         96/124  77%\nmerged, no rework  68/96   71%\n"
                    "cohort: opened 14+ days ago (UTC dates)\n\n"
                    "today    +0 opened  +1 merged  +0 closed\n"
                    "7 days   +0 opened  +4 merged  +0 closed\n")
        card = site.status_card(self.fixture())
        self.assertEqual(card, expected)
        self.assertLessEqual(max(map(len, card.splitlines())), 44)

    def test_status_card_small_sample_and_failed_observation(self):
        data = self.fixture()
        for key in ("cohort_merge_rate", "merged_no_rework_share"):
            data["metrics"][key].update(value=None, numerator=7, denominator=9)
        data["activity"]["observation"] = {"ok": False, "checked_at": "2026-11-20T17:01:00Z"}
        card = site.status_card(data)
        self.assertEqual(card.count("n<20"), 2)
        self.assertEqual(card.count("7/9"), 2)
        self.assertNotIn("%", card)
        self.assertTrue(card.endswith("not refreshed since 2026-11-20\n"))
        self.assertLessEqual(max(map(len, card.splitlines())), 44)

    def test_status_card_width_stays_bounded_for_larger_daily_counts(self):
        data = self.fixture()
        for row in data['activity']['days'][-7:]:
            row.update(opened=100, merged=100, closed_unmerged=100)
        card = site.status_card(data)
        self.assertLessEqual(max(map(len, card.splitlines())), 44)
        for value in ('100', '700'):
            self.assertEqual(card.count('+' + value), 3)


class PageIntegrityTests(unittest.TestCase):
    def test_figures_and_activity_are_deterministic_well_formed_xml(self):
        data = projection()
        before = copy.deepcopy(data)
        figures = site.figures(data)
        names = {f"{name}-{theme}.svg" for name in ("line", "board", "gate", "profile", "activity")
                 for theme in ("light", "dark")}
        names.update(f"profile-{theme}-compact.svg" for theme in ("light", "dark"))
        self.assertEqual(set(figures), names)
        self.assertEqual(figures, site.figures(data))
        for name, text in figures.items():
            with self.subTest(name=name):
                root = ET.fromstring(text)
                self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
                self.assertEqual(root.find("{*}title").text, root.attrib["aria-label"])
        for theme in ("light", "dark"):
            text = site.activity(data, theme)
            self.assertEqual(text, figures[f"activity-{theme}.svg"])
            label = ET.fromstring(text).attrib["aria-label"]
            self.assertIn("0 pull requests opened, 24 merged and 0 closed without merge", label)
            self.assertIn("2026-10-25 to 2026-11-21", label)
        self.assertEqual(data, before)

    def test_pages_are_stable_escape_projection_text_and_load_no_remote_resources(self):
        data = projection()
        hostile = '<script>"quoted" & \'single\'</script>'
        data["pull_requests"][0]["repository"] = hostile
        data["by_repository"][0]["repository"] = hostile
        data["metrics"]["prs_merged"]["reason"] = hostile
        data["improvements"] = [{"date": "2026-11-20", "happened": hostile, "change": hostile,
                                 "moves": hostile, "links": []}]
        before = copy.deepcopy(data)
        for render in (site.render, site.render_board):
            with self.subTest(render=render.__name__):
                text = render(data, CONFIG)
                self.assertEqual(text, render(data, CONFIG))
                self.assertIsNone(site.SLOT.search(text))
                self.assertNotIn(hostile, text)
                self.assertIn(site.escape(hostile), text)
                parsed = Fragment(text)
                for tag, attrs in parsed.tags:
                    if tag == "a":
                        continue
                    for attribute in ("src", "href", "srcset", "data", "poster"):
                        self.assertNotRegex(attrs.get(attribute, ""), r"(?i)^(?:https?:)?//")
                self.assertNotRegex(text, r'(?i)(?:url\(\s*[\"\']?|@import\s*[\"\'])(?:https?:)?//')
                policy = next(attrs["content"] for tag, attrs in parsed.tags
                              if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy")
                scripts = re.findall(r"<script([^>]*)>(.*?)</script>", text, re.S)
                executable = [body for attrs, body in scripts if "application/json" not in attrs]
                self.assertEqual(len(executable), 1)
                digest = base64.b64encode(hashlib.sha256(executable[0].encode()).digest()).decode()
                self.assertIn("script-src 'sha256-" + digest + "'", unescape(policy))
                for attrs, body in scripts:
                    if "application/json" in attrs:
                        self.assertEqual(json.loads(body), data)
        self.assertEqual(data, before)
