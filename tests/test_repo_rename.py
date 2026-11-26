import datetime as dt
import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import DEFAULTS, setup_module

from factory import cli, util  # noqa: E402
from factory.core import repos  # noqa: E402
from factory.oss import plan, profile_readme, retro, submit  # noqa: E402
from factory.util import CONFIG, CommandError, GH_READ_TIMEOUT, write_json  # noqa: E402

AT = dt.datetime(2026, 11, 4, 12, tzinfo=dt.timezone.utc)
RENAMES = {"facebook/lexical": "meta/lexical"}


def setUpModule():
    setup_module(unittest)


def ago(days):
    return (AT - dt.timedelta(days=days)).isoformat()


def row(repo, number, status, submitted=None, merged=None):
    data = {"url": f"https://github.com/{repo}/pull/{number}", "repo": repo, "number": number, "status": status,
            "submitted_at": submitted or ago(9)}
    if merged:
        data["merged_at"] = merged
    return data


LEXICAL = [row("facebook/lexical", 9464, "merged", ago(10), ago(6)),
           row("meta/lexical", 9669, "ci-failing", ago(4)),
           row("meta/lexical", 9676, "in-review", ago(4)),
           row("meta/lexical", 9677, "ci-failing", ago(4))]


class Workspace(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name).resolve()
        self.ledger = self.root / "ledger" / "prs.json"
        self.ledger.parent.mkdir()
        write_json(self.ledger, [])
        for mocker in (patch.dict(CONFIG, {"runs_dir": str(self.root / "runs"), "ledger_file": str(self.ledger),
                                           "repo_renames": dict(RENAMES)}),
                       patch.object(util, "run", side_effect=AssertionError("subprocess")),
                       patch.object(plan, "now", return_value=AT)):
            mocker.start()
            self.addCleanup(mocker.stop)

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, json.loads(out.getvalue())


class Canonical(Workspace):
    def test_shipped_config_maps_facebook_lexical_to_meta_lexical(self):
        self.assertEqual(DEFAULTS["repo_renames"], RENAMES)

    def test_old_names_map_case_insensitively_and_unknown_names_pass_through(self):
        self.assertEqual(repos.canonical("facebook/lexical"), "meta/lexical")
        self.assertEqual(repos.canonical("Facebook/Lexical"), "meta/lexical")
        self.assertEqual(repos.canonical("meta/lexical"), "meta/lexical")
        self.assertEqual(repos.canonical("Some/Repo"), "Some/Repo")
        self.assertEqual(repos.canonical(""), "")
        self.assertIsNone(repos.canonical(None))
        del CONFIG["repo_renames"]
        self.assertEqual(repos.canonical("facebook/lexical"), "facebook/lexical")

    def test_a_map_it_cannot_apply_is_refused_and_plan_stops(self):
        write_json(self.ledger, LEXICAL)
        for table, why in (([["facebook/lexical", "meta/lexical"]], "must be an object"),
                           ({"facebook/lexical": "lexical"}, "both must be owner/name"),
                           ({"facebook lexical": "meta/lexical"}, "both must be owner/name"),
                           ({"facebook/lexical": None}, "both must be owner/name"),
                           ({"Facebook/Lexical": "meta/lexical", "facebook/lexical": "other/lexical"}, "twice"),
                           ({"a/old": "b/mid", "b/mid": "c/new"}, "map a/old straight to the current name")):
            CONFIG["repo_renames"] = table
            with self.subTest(table=table):
                with self.assertRaises(SystemExit) as raised:
                    repos.canonical("x/y")
                self.assertIn(why, str(raised.exception))
                with self.assertRaises(SystemExit) as raised:
                    plan.run()
                self.assertIn(why, str(raised.exception))


class Plan(Workspace):
    def test_repo_slots_count_both_names_under_the_current_one(self):
        write_json(self.ledger, LEXICAL + [row("o/other", 1, "merged", ago(9), ago(8))])
        slots = {s["repo"]: s for s in plan.run()["repo_slots"]}
        self.assertEqual(sorted(slots), ["meta/lexical", "o/other"])
        lexical = slots["meta/lexical"]
        self.assertEqual({k: lexical[k] for k in ("open", "merged", "oldest_open_days", "stopped")},
                         {"open": 3, "merged": 1, "oldest_open_days": 4.0, "stopped": False})
        self.assertEqual(lexical["stop_reasons"], [])

    def test_a_merge_under_the_current_name_clears_open_prs_under_the_old_one(self):
        write_json(self.ledger, [*[row("facebook/lexical", n, "silent", ago(40)) for n in (1, 2, 3)],
                                 row("Meta/Lexical", 4, "merged", ago(3), ago(1))])
        (lexical,) = plan.run()["repo_slots"]
        self.assertEqual({k: lexical[k] for k in ("repo", "open", "merged", "stopped", "stop_reasons")},
                         {"repo": "meta/lexical", "open": 3, "merged": 1, "stopped": False, "stop_reasons": []})


class Influence(Workspace):
    def setUp(self):
        super().setUp()
        self.calls = []
        mocker = patch.object(plan, "gh_json", side_effect=self.respond)
        mocker.start()
        self.addCleanup(mocker.stop)
        write_json(self.ledger, LEXICAL + [row("meta/lexical", 9700, "merged", ago(3), ago(2))])
        self.history = self.ledger.with_name("influence.jsonl")

    def respond(self, *args, **kwargs):
        self.calls.append(args[1])
        if args[1].startswith("users/"):
            return {"followers": 5, "public_repos": 3}
        if args[1] == "repos/meta/lexical":
            return 23000
        if args[1].startswith("repos/meta/lexical/contributors"):
            return [{"login": "fixture-owner"}]
        raise CommandError(f"gh api {args[1]} failed (1): HTTP 404")

    def record(self, *entries):
        self.history.write_text(json.dumps({"at": ago(1), "followers": 5, "repos": list(entries)}) + "\n")

    def test_merges_under_both_names_are_one_reading_against_history_under_the_old_name(self):
        self.record({"repo": "facebook/lexical", "merged": 1, "rank": 50})
        report = plan.influence()
        self.assertEqual([(r["repo"], r["merged"], r["rank"]) for r in report["repos"]], [("meta/lexical", 2, 1)])
        self.assertEqual(report["repos"][0]["delta"], {"merged": 1, "rank": -49, "previous_rank": 50})
        self.assertFalse(any("facebook" in call for call in self.calls))

    def test_history_listing_both_names_is_summed_before_the_delta(self):
        self.record({"repo": "facebook/lexical", "merged": 1, "rank": None}, {"repo": "meta/lexical", "merged": 1, "rank": 50})
        report = plan.influence()
        self.assertEqual([(r["repo"], r["merged"]) for r in report["repos"]], [("meta/lexical", 2)])
        self.assertEqual(report["repos"][0]["delta"], {"merged": 0, "rank": -49, "previous_rank": 50})


class Unresponsive(Workspace):
    def setUp(self):
        super().setUp()
        self.waiting = [row(name, n, "silent", ago(40)) for n, name in enumerate(
            ("Facebook/Lexical", "meta/lexical", "Meta/Lexical"), 1)]
        write_json(self.ledger, self.waiting)
        no_network = patch.object(submit, "gh_json", side_effect=AssertionError("no GitHub read"))
        no_network.start()
        self.addCleanup(no_network.stop)

    def test_unanswered_prs_under_both_names_stop_old_and_current_items(self):
        expected = plan.unresponsive(self.waiting, at=AT)
        self.assertEqual(len(expected), 1)
        for name in ("facebook/lexical", "Facebook/Lexical", "meta/lexical", "Meta/Lexical"):
            with self.subTest(name=name):
                self.assertEqual(submit.unresponsive(name), expected)
        self.assertEqual(submit.unresponsive("other/repo"), [])

    def test_a_recent_merge_under_either_name_clears_the_same_repository(self):
        for name in ("facebook/lexical", "Meta/Lexical"):
            with self.subTest(name=name):
                write_json(self.ledger, self.waiting + [row(name, 4, "merged", ago(3), ago(1))])
                self.assertEqual(submit.unresponsive("meta/lexical"), [])
                self.assertEqual(submit.unresponsive("facebook/lexical"), [])

    def test_an_answer_in_another_repository_does_not_clear_the_pause(self):
        write_json(self.ledger, self.waiting + [row("other/repo", 4, "in-review", ago(1))])
        self.assertEqual(submit.unresponsive("meta/lexical"), plan.unresponsive(self.waiting, at=AT))


class ProfileCheck(Workspace):
    def setUp(self):
        super().setUp()
        mocker = patch.object(profile_readme, "gh_json", side_effect=AssertionError("network"))
        mocker.start()
        self.addCleanup(mocker.stop)

    def check(self, readme, rows):
        write_json(self.ledger, rows)
        path = self.root / "README.md"
        path.write_text(readme)
        return self.run_cli("profile-check", "--readme", str(path))

    def test_a_merge_under_the_old_name_is_listed_by_a_link_to_the_current_name(self):
        code, result = self.check("- [Lexical](https://github.com/meta/lexical/pull/9464): table selection\n", LEXICAL)
        self.assertEqual((code, result["missing_repos"], result["unlisted_prs"]), (0, [], []))

    def test_counts_and_pull_links_cover_merges_under_both_names(self):
        rows = LEXICAL + [row("meta/lexical", 9700, "merged")]
        code, result = self.check("- [Lexical](https://github.com/meta/lexical/pulls?q=is%3Amerged) (2): tables\n", rows)
        self.assertEqual((code, result["count_mismatch"]), (0, []))
        code, result = self.check("- [Lexical](https://github.com/facebook/lexical/pull/9700): tables\n", rows)
        self.assertEqual(code, 1)
        self.assertEqual(result["missing_repos"], [])
        self.assertEqual(result["unlisted_prs"], [{"repo": "meta/lexical", "linked": [9700], "unlisted": [9464]}])


class Retro(Workspace):
    def test_by_repo_folds_the_old_name_into_the_current_one(self):
        def pr(repo, stars, created, merged=None, state="OPEN"):
            return {"url": f"https://github.com/{repo}/pull/1", "title": "t", "state": state, "createdAt": created,
                    "mergedAt": merged, "closedAt": merged, "additions": 5, "deletions": 0,
                    "repository": {"nameWithOwner": repo, "stargazerCount": stars}}

        prs = [pr("facebook/lexical", 23000, ago(10), ago(6), "MERGED"), pr("meta/lexical", 23000, ago(4)),
               pr("o/other", 10, ago(4))]
        out = retro.summarize(prs, now=AT)
        by = {r["repo"]: r for r in out["by_repo"]}
        self.assertEqual(sorted(by), ["meta/lexical", "o/other"])
        self.assertEqual((by["meta/lexical"]["merged"], by["meta/lexical"]["open"]), (1, 1))
        self.assertEqual(out["single_merge_big_repos"], ["meta/lexical"])
        self.assertEqual(out["no_merge_repos"], ["o/other"])


class Check(Workspace):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.current = {"facebook/lexical": "meta/lexical", "meta/lexical": "meta/lexical", "old/tool": "new-owner/tool",
                        "Gamma/Lib": "gamma/lib", "odd/repo": None}
        mocker = patch.object(repos, "gh_json", side_effect=self.respond)
        mocker.start()
        self.addCleanup(mocker.stop)

    def respond(self, *args, **kwargs):
        self.calls.append(args[1])
        self.assertEqual(kwargs, {"timeout": GH_READ_TIMEOUT})
        self.assertEqual((len(args), args[0]), (2, "api"))
        name = args[1].removeprefix("repos/")
        if name not in self.current:
            raise CommandError(f"gh api {args[1]} failed (1): HTTP 404")
        return {"full_name": self.current[name], "name": name.split("/")[1]}

    def test_reports_renames_the_map_lacks_and_never_edits_the_config(self):
        write_json(self.ledger, LEXICAL + [row("old/tool", 1, "merged"), row("Gamma/Lib", 2, "merged"), row("gamma/lib", 3, "closed")])
        config = (ROOT / "factory.config.json").read_bytes()
        code, result = self.run_cli("repo-renames", "--check")
        self.assertEqual(code, 1)
        self.assertEqual(result, {"ok": False, "checked": 4, "errors": [],
                                  "unmapped": [{"repo": "old/tool", "maps_to": "old/tool", "current": "new-owner/tool"}]})
        self.assertEqual(sorted(self.calls), ["repos/Gamma/Lib", "repos/facebook/lexical", "repos/meta/lexical", "repos/old/tool"])
        self.assertEqual((ROOT / "factory.config.json").read_bytes(), config)
        self.assertEqual(CONFIG["repo_renames"], RENAMES)

    def test_passes_when_every_ledger_repo_resolves_to_its_mapped_name(self):
        write_json(self.ledger, LEXICAL)
        code, result = self.run_cli("repo-renames", "--check")
        self.assertEqual((code, result), (0, {"ok": True, "checked": 2, "unmapped": [], "errors": []}))

    def test_a_map_entry_that_no_longer_matches_github_is_reported(self):
        CONFIG["repo_renames"] = {"facebook/lexical": "meta-old/lexical"}
        write_json(self.ledger, LEXICAL)
        code, result = self.run_cli("repo-renames", "--check")
        self.assertEqual(code, 1)
        self.assertEqual(result["unmapped"], [{"repo": "facebook/lexical", "maps_to": "meta-old/lexical", "current": "meta/lexical"}])

    def test_a_failed_lookup_fails_the_check_instead_of_passing_the_repo(self):
        write_json(self.ledger, LEXICAL + [row("gone/repo", 1, "closed"), row("odd/repo", 2, "closed")])
        code, result = self.run_cli("repo-renames", "--check")
        self.assertEqual((code, result["ok"], result["unmapped"]), (1, False, []))
        errors = {e["repo"]: e["error"] for e in result["errors"]}
        self.assertEqual(sorted(errors), ["gone/repo", "odd/repo"])
        self.assertIn("HTTP 404", errors["gone/repo"])
        self.assertIn("without a full_name", errors["odd/repo"])

    def test_refuses_without_a_usable_ledger_or_map_before_any_lookup(self):
        for setup, why in ((lambda: self.ledger.unlink(), "not found"),
                           (lambda: self.ledger.write_text("{not json"), "unreadable"),
                           (lambda: write_json(self.ledger, [{"url": "https://github.com/a/b/pull/1"}]), "without a repo name"),
                           (lambda: CONFIG.update(repo_renames={"a/old": "b/mid", "b/mid": "c/new"}), "straight to the current name")):
            write_json(self.ledger, LEXICAL)
            setup()
            with self.subTest(why=why), self.assertRaises(SystemExit) as raised, redirect_stdout(io.StringIO()):
                cli.main(["repo-renames", "--check"])
            self.assertIn(why, str(raised.exception))
            CONFIG["repo_renames"] = dict(RENAMES)
        self.assertEqual(self.calls, [])

    def test_without_check_prints_the_map_and_calls_nothing(self):
        self.assertEqual(self.run_cli("repo-renames"), (0, {"repo_renames": RENAMES}))
        CONFIG["repo_renames"] = {"facebook/lexical": "lexical"}
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            cli.main(["repo-renames"])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
