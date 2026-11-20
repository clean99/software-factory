"""Public snapshots are allowlisted, scanned and independent of private history."""
import hashlib
import io
import json
import pathlib
import shlex
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from support import DEFAULTS, setup_module
from factory import cli
from factory.core import privacy, privacy_git, public, publication, slop


def setUpModule():
    setup_module(unittest)


class PublicExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name).resolve()
        self.source = self.root / "private"
        self.source.mkdir()
        self.out = self.root / "export"
        self.config = {"github_user": "fixture-owner", "git_name": "Contributor",
                       "git_email": "contributor@users.noreply.github.com",
                       "public": {"repository": "example-org/software-factory",
                                  "dashboard_url": "https://example-org.github.io/software-factory/"},
                       "limits": {"max_diff_lines": 400, "new_text_field": "private-marker"}}
        self.rules = {"files": ["factory/demo.py", "bin/demo"], "scrub": [["old-notebook", "notebook"]],
                      "readme_template": "readme.txt", "generated": {"ledger/prs.json": "[]\n"},
                      "tests": {"command": "python3 tests/run_suite.py --jobs 4"}}
        self.write("factory/demo.py", 'LABEL = "old-notebook"\n')
        self.write("bin/demo", "#!/bin/sh\nexit 0\n").chmod(0o755)
        self.write("readme.txt", "# Software Factory\n{repository}\n{dashboard_url}\n")
        self.write("runs/private.txt", "private-marker")
        self.write(".factory.local.json", '{"private": "private-marker"}')
        self.policy = patch.object(public, "policy", return_value=self.rules)
        self.policy.start()
        self.addCleanup(self.policy.stop)
        self.configuration = patch.dict(public.CONFIG, self.config)
        self.configuration.start()
        self.addCleanup(self.configuration.stop)
        self.scanner = patch.object(public.leaks, "scan", return_value=[])
        self.scanner.start()
        self.addCleanup(self.scanner.stop)

    def write(self, name, text):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def export(self, **kwargs):
        return public.build(self.out, root=self.source, config=self.config, rules=self.rules, **kwargs)

    def test_allowlist_omits_private_records_and_unlisted_files(self):
        self.write("factory/not-selected.py", 'VALUE = "private-marker"')
        result = self.export()
        self.assertTrue(result["ok"], result)
        self.assertFalse((self.out / "runs").exists())
        self.assertFalse((self.out / ".factory.local.json").exists())
        self.assertFalse((self.out / "factory/not-selected.py").exists())
        self.assertEqual((self.out / "factory/demo.py").read_text(), 'LABEL = "notebook"\n')
        self.assertTrue((self.out / "bin/demo").stat().st_mode & 0o111)
        self.assertEqual(result["bytes"], sum(p.stat().st_size for p in self.out.rglob("*") if p.is_file()))
        self.assertEqual(result["files"], len([p for p in self.out.rglob("*") if p.is_file()]))

    def test_configuration_is_rebuilt_not_blacklisted(self):
        self.config.update(standing_approval_note="private-marker", future_secret="private-marker",
                           repos_dir="machine-path", private_terms=["private-marker"])
        self.assertTrue(self.export()["ok"])
        config = json.loads((self.out / "factory.config.json").read_text())
        self.assertEqual(config["repos_dir"], "workspace/repos")
        self.assertNotIn("standing_approval_note", config)
        self.assertNotIn("future_secret", config)
        self.assertNotIn("private_terms", config)
        self.assertEqual(config["limits"], {"max_diff_lines": 400})
        self.assertNotIn("fixture-owner", json.dumps(config))
        self.assertIn(self.config["public"]["dashboard_url"], (self.out / "README.md").read_text())

    def test_public_policy_does_not_retain_private_scrub_names(self):
        self.rules["private_reasons"] = {"private-notes": "private-marker"}
        self.assertTrue(self.export()["ok"])
        policy = json.loads((self.out / "factory/core/public-export.json").read_text())
        self.assertEqual(policy["scrub"], [])
        self.assertNotIn("private_reasons", policy)
        self.assertNotIn("old-notebook", json.dumps(policy))

    def test_existing_destination_is_never_overwritten(self):
        self.out.mkdir()
        sentinel = self.out / "keep"
        sentinel.write_text("keep")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.export()
        self.assertEqual(sentinel.read_text(), "keep")

    def test_output_cannot_overlap_private_checkout(self):
        for path in (self.source, self.source / "out", self.root):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "separate"):
                public.build(path, root=self.source, config=self.config, rules=self.rules)

    def test_missing_allowlisted_input_fails(self):
        self.rules["files"].append("factory/missing.py")
        with self.assertRaises(FileNotFoundError):
            self.export()
        self.assertFalse(self.out.exists())

    def test_source_symlink_and_directory_symlink_fail_closed(self):
        (self.source / "factory/demo.py").unlink()
        (self.source / "factory/demo.py").symlink_to(self.source / "runs/private.txt")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.export()
        (self.source / "factory/demo.py").unlink()
        (self.source / "factory").rmdir()
        (self.source / "factory").symlink_to(self.source / "runs", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.export()

    def test_path_traversal_and_git_metadata_are_rejected(self):
        for name in ("../escape", "/absolute", ".git/config", "factory/../../escape"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                public.regular_file(self.source, name)
            with self.subTest(name=name), self.assertRaises(ValueError):
                public.write_file(self.root / "new", name, "text")

    def test_binary_and_oversized_inputs_fail(self):
        self.write("factory/demo.py", "\0binary")
        with self.assertRaisesRegex(ValueError, "binary"):
            self.export()
        self.write("factory/demo.py", "x" * (public.MAX_FILE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "bounded"):
            self.export()

    def test_secret_scanner_failure_blocks_and_removes_staging(self):
        with patch.object(public.leaks, "scan", return_value=["scanner unavailable"]):
            result = self.export()
        self.assertFalse(result["ok"])
        self.assertEqual(result["findings"][0]["scanner"], "gitleaks")
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.root.glob(".public-export-*")), [])

    def test_private_marker_in_content_or_filename_blocks(self):
        self.config["private_terms"] = ["private-marker"]
        self.write("factory/demo.py", 'VALUE = "private-marker"')
        result = self.export()
        self.assertFalse(result["ok"])
        self.assertFalse(self.out.exists())
        self.write("factory/demo.py", 'VALUE = "safe"')
        self.write("factory/private-marker.py", "pass")
        self.rules["files"].append("factory/private-marker.py")
        self.assertFalse(self.export()["ok"])

    def test_reviewed_source_exception_is_file_and_full_line_bound(self):
        line = "pathlib.Path.home()"
        text = line + "\n"
        self.write("factory/demo.py", text)
        row = {"category": "internal hostname", "sha256": hashlib.sha256(line.encode()).hexdigest(),
               "reason": "Python method call, not a host."}
        self.rules["reviewed_source_lines"] = {"factory/demo.py": [row]}
        self.assertTrue(self.export()["ok"])
        self.assertTrue(public.scan_text("other.py", text))
        self.assertTrue(public.scan_text("factory/demo.py", line + ' + "extra"', [row]))
        self.assertTrue(public.scan_text("factory/demo.py", line + "\r" + line, [row]))
        self.assertEqual(public.scan_text("factory/demo.py", text, [row]), [])
        with patch.object(public.leaks, "scan", return_value=["secret scanner finding"]):
            self.assertTrue(public.scan_text("factory/demo.py", text, [row]))

    def test_optional_metrics_are_still_scanned(self):
        self.config["private_terms"] = ["private-marker"]
        with patch.object(public, "metrics_files", return_value=({"site/metrics.json": '{"leak":"private-marker"}'}, "included")):
            self.assertFalse(self.export()["ok"])
        self.assertFalse(self.out.exists())
        with patch.object(public, "metrics_files", return_value=({"site/metrics.json": '{"merged":0}'}, "included")):
            result = self.export()
        self.assertTrue(result["ok"])
        self.assertEqual(result["metrics"], "included")
        self.assertEqual(json.loads((self.out / "site/metrics.json").read_text()), {"merged": 0})

    def test_metrics_adapter_reads_separate_records_without_exporting_or_mutating_them(self):
        self.write("factory/core/metrics.py", "# Installed adapter marker\n")
        self.write("factory.config.json", json.dumps(self.config))
        self.write("ledger/prs.json", "[]")
        records = self.root / "records"
        records.mkdir()
        (records / "factory.config.json").write_text(json.dumps({"ledger_file": "prs.json"}))
        rows = [{"url": f"https://github.com/example/project/pull/{n}", "status": "merged",
                 "submitted_at": "2026-01-01T00:00:00Z", "merged_at": "2026-01-02T00:00:00Z",
                 "note": "private-marker"} for n in (1, 2)]
        (records / "prs.json").write_text(json.dumps(rows))
        report = records / "suite.json"
        report.write_text(json.dumps({"success": True, "tests": 2, "wall_seconds": 3,
                                      "modules": [{"tests": 2, "seconds": 3, "exit_code": 0}]}))
        before = {p: p.read_bytes() for p in records.iterdir()}
        self.config["private_terms"] = ["private-marker"]
        result = self.export(records_root=records, suite_report=report)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["metrics"], "included")
        data = json.loads((self.out / "site/metrics.json").read_text())
        self.assertEqual(data["metrics"]["prs_merged"]["value"], 2)
        self.assertEqual(data["metrics"]["factory_test_count"]["value"], 2)
        self.assertEqual(data["links"]["dashboard"], self.config["public"]["dashboard_url"])
        self.assertIn(self.config["public"]["dashboard_url"], (self.out / "site/index.html").read_text())
        self.assertEqual(json.loads((self.out / "ledger/prs.json").read_text()), [])
        self.assertEqual({p: p.read_bytes() for p in records.iterdir()}, before)
        self.assertNotIn("private-marker", (self.out / "site/metrics.json").read_text())
        self.assertNotIn(str(records), (self.out / "site/index.html").read_text())

    def test_installed_metrics_without_adapter_blocks_export(self):
        from factory.core import metrics

        self.write("factory/core/metrics.py", "# Installed adapter marker\n")
        with patch.object(metrics, "public_files", None), self.assertRaisesRegex(ValueError, "no public export adapter"):
            self.export()
        self.assertFalse(self.out.exists())

    def test_public_configuration_keeps_disclosure_cutoff_and_shared_links(self):
        from factory.core import metrics
        from factory.oss import disclosure, profile_readme

        self.config["public"]["disclosure_since"] = "2026-11-20"
        projected = public.public_config(self.config)
        self.assertEqual(projected["public"], self.config["public"])
        with patch.dict(public.CONFIG, projected, clear=True):
            url = "https://github.com/" + projected["public"]["repository"]
            self.assertIn(url, disclosure.default_line())
            self.assertIn(url, profile_readme.portfolio([], "fixture-owner"))
            self.assertEqual(metrics.public_links(projected)["repository"], url)
        self.assertNotIn("portfolio", projected)
        self.assertNotIn("public_repository", projected)
        self.assertNotIn("dashboard_url", projected)

    def test_metrics_cannot_replace_code_or_escape(self):
        for name in ("factory/demo.py", "../escape", ".git/config"):
            with self.subTest(name=name), patch.object(public, "metrics_files", return_value=({name: "bad"}, "included")), \
                    self.assertRaises(ValueError):
                self.export()
        self.assertFalse(self.out.exists())

    def test_destination_url_is_configured_once_and_validated(self):
        self.config["public"]["dashboard_url"] = "https://example.org/elsewhere"
        with self.assertRaisesRegex(ValueError, "GitHub Pages"):
            self.export()

    def test_cli_export_and_dry_run_dispatch(self):
        with patch.object(public, "build", return_value={"ok": True}) as build, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["public", "export", "--out", str(self.out)]), 0)
        build.assert_called_once_with(self.out)
        with patch.object(public, "CONFIG", self.config), patch.object(public, "build", return_value={"ok": True}), \
                patch.object(public, "snapshot_plan", return_value={"ok": True}) as plan, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["public", "publish", "--dry-run"]), 0)
        plan.assert_called_once()

    def test_publish_requires_dry_run_and_configured_repo(self):
        with patch.object(public, "CONFIG", self.config):
            with self.assertRaisesRegex(SystemExit, "dry-run only"):
                cli.main(["public", "publish"])
            with self.assertRaisesRegex(SystemExit, "must match"):
                cli.main(["public", "publish", "--dry-run", "--repo", "other/project"])

    def git(self, path, *args):
        env = public.os.environ.copy()
        env.update(GIT_AUTHOR_NAME="Contributor", GIT_COMMITTER_NAME="Contributor",
                   GIT_AUTHOR_EMAIL="contributor@users.noreply.github.com",
                   GIT_COMMITTER_EMAIL="contributor@users.noreply.github.com")
        return subprocess.check_output(["git", "-C", str(path), *args], env=env, stderr=subprocess.DEVNULL).decode().strip()

    def repository(self, name, message):
        path = self.root / name
        path.mkdir()
        self.git(path, "init", "--initial-branch=main")
        (path / "README.md").write_text("Public fixture\n")
        self.git(path, "add", "README.md")
        self.git(path, "commit", "-m", message)
        return path

    def test_first_snapshot_is_a_root_not_private_history(self):
        self.assertTrue(self.export()["ok"])
        plan = public.snapshot_plan(self.out, config=self.config)
        self.assertTrue(plan["dry_run"])
        self.assertIsNone(plan["parent"])
        self.assertEqual(plan["commits"], 1)
        self.assertIn(public.SNAPSHOT_MARKER, plan["message"])
        self.assertEqual(plan["push_argv"][:2], ["bin/factory", "push"])
        self.assertEqual(plan["push_argv"][-1], "HEAD:refs/heads/main")
        self.assertFalse(plan["commit_retained"])

    def test_inherited_git_directories_cannot_redirect_the_snapshot_commit(self):
        private = self.repository("unrelated", "Private history")
        before = self.git(private, "rev-parse", "HEAD")
        self.assertTrue(self.export()["ok"])
        with patch.dict(public.os.environ, {"GIT_DIR": str(private / ".git"),
                                           "GIT_WORK_TREE": str(private),
                                           "GIT_INDEX_FILE": str(self.root / "foreign-index")}):
            plan = public.snapshot_plan(self.out, config=self.config)
        self.assertIsNone(plan["parent"])
        self.assertEqual(self.git(private, "rev-parse", "HEAD"), before)
        self.assertEqual(self.git(private, "status", "--porcelain"), "")
        self.assertFalse((self.root / "foreign-index").exists())

    def test_existing_local_bare_repo_gets_a_snapshot_parent_without_writes(self):
        fixture = self.repository("public", "First snapshot\n\n" + public.SNAPSHOT_MARKER)
        before = self.git(fixture, "rev-parse", "HEAD")
        bare = self.root / "public.git"
        self.git(self.root, "clone", "--bare", str(fixture), str(bare))
        self.assertTrue(self.export()["ok"])
        plan = public.snapshot_plan(self.out, clone=bare, config=self.config)
        self.assertEqual(plan["parent"], before)
        self.assertEqual(plan["commits"], 2)
        self.assertNotEqual(plan["head"], before)
        self.assertEqual(self.git(bare, "rev-parse", "HEAD"), before)
        self.assertEqual(self.git(fixture, "status", "--porcelain"), "")

    def test_snapshot_marker_cannot_hide_private_history_content(self):
        fixture = self.repository("tainted", "Snapshot\n\n" + public.SNAPSHOT_MARKER)
        (fixture / "README.md").write_text("private-marker")
        self.git(fixture, "add", "README.md")
        self.git(fixture, "commit", "-m", "Snapshot\n\n" + public.SNAPSHOT_MARKER)
        (fixture / "README.md").write_text("Clean current content")
        self.git(fixture, "add", "README.md")
        self.git(fixture, "commit", "-m", "Snapshot\n\n" + public.SNAPSHOT_MARKER)
        self.config["private_terms"] = ["private-marker"]
        self.assertTrue(self.export()["ok"])
        with self.assertRaisesRegex(ValueError, "file scan finding"):
            public.snapshot_plan(self.out, clone=fixture, config=self.config)

    def test_snapshot_input_is_rescanned_after_export(self):
        self.assertTrue(self.export()["ok"])
        (self.out / "README.md").write_text("private-marker")
        self.config["private_terms"] = ["private-marker"]
        with self.assertRaisesRegex(ValueError, "scan findings"):
            public.snapshot_plan(self.out, config=self.config)

    def test_deleted_unlisted_paths_remain_forbidden_in_snapshot_history(self):
        self.assertTrue(self.export()["ok"])
        for name in ("docs/owner-actions.md", "docs/interview/story.md", "runs/sample/evidence.txt",
                     "factory/unreviewed.py", "site/private.txt"):
            with self.subTest(name=name):
                fixture = self.repository("history-" + str(len(list(self.root.iterdir()))), public.SNAPSHOT_MARKER)
                path = fixture / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Ordinary synthetic notes\n")
                self.git(fixture, "add", name)
                self.git(fixture, "commit", "-m", public.SNAPSHOT_MARKER)
                path.unlink()
                self.git(fixture, "add", "--all")
                self.git(fixture, "commit", "-m", public.SNAPSHOT_MARKER)
                with self.assertRaisesRegex(ValueError, "unlisted public path"):
                    public.snapshot_plan(self.out, clone=fixture, config=self.config)

    def test_current_snapshot_rejects_unlisted_paths(self):
        self.assertTrue(self.export()["ok"])
        (self.out / "unreviewed.txt").write_text("Ordinary synthetic notes\n")
        with self.assertRaisesRegex(ValueError, "unlisted public path"):
            public.snapshot_plan(self.out, config=self.config)

    def test_reviewed_detector_with_local_markers_and_plain_scan(self):
        name = "factory/core/privacy.py"
        line = next(line for line in pathlib.Path(privacy.__file__).read_text().splitlines()
                    if line.startswith("LARK ="))
        self.rules["files"].append(name)
        self.write(name, line + "\n")
        trusted = json.loads(public.POLICY.read_text())["reviewed_source_lines"]
        self.rules["reviewed_source_lines"] = {name: trusted[name]}
        config = {**self.config, "private_terms": ["mindnotes"], "private_repos": []}
        with patch.dict(privacy.CONFIG, config), patch.object(privacy, "identity_markers", return_value=["mindnotes"]):
            result = public.build(self.out, root=self.source, config=config, rules=self.rules)
            self.assertTrue(result["ok"], result)
            self.assertTrue(public.content_findings("factory/other.py", line, self.rules, config))
            self.assertTrue(public.content_findings(name, line + " # changed", self.rules, config))
            self.assertTrue(public.content_findings(name, line + '\nVALUE = "mindnotes"', self.rules, config))

    def test_snapshot_plan_runs_real_push_preflight_with_trusted_literals(self):
        line = "pathlib.Path.home()"
        self.write("factory/demo.py", line + "\n")
        self.rules["reviewed_source_lines"] = {"factory/demo.py": [{
            "category": "internal hostname", "sha256": hashlib.sha256(line.encode()).hexdigest(),
            "reason": "Python expression, not a host."}]}
        self.assertTrue(self.export()["ok"])
        with patch.object(publication, "check_push", wraps=publication.check_push) as check, \
                patch.object(publication, "planned_push", wraps=publication.planned_push) as planned:
            result = public.snapshot_plan(self.out, config=self.config)
        self.assertTrue(result["ok"])
        self.assertEqual(check.call_count, 1)
        self.assertEqual(planned.call_count, 1)
        self.assertTrue(result["push_preflight"]["ok"])

    def test_wrapper_does_not_trust_exceptions_from_the_outgoing_manifest(self):
        fixture = self.repository("forged-exception", public.SNAPSHOT_MARKER)
        line = "pathlib.Path.home()"
        (fixture / "README.md").write_text(line)
        (fixture / "public-export.json").write_text(json.dumps({"reviewed_source_lines": {
            "README.md": [{"category": "internal hostname", "sha256": hashlib.sha256(line.encode()).hexdigest(),
                           "reason": "Untrusted claim"}]}}))
        self.git(fixture, "add", "--all")
        self.git(fixture, "commit", "-m", public.SNAPSHOT_MARKER)
        destination = self.root / "empty.git"
        self.git(self.root, "init", "--bare", str(destination))
        url = "https://github.com/" + self.config["public"]["repository"] + ".git"
        plan = [(url, "*", "HEAD", "refs/heads/main", "[new branch]")]
        with self.assertRaisesRegex(ValueError, "file scan finding"):
            publication.check_push([url, "HEAD:refs/heads/main"], fixture, plan=plan, advertised={url: {}})

    def test_wrapper_limits_source_exceptions_to_the_configured_public_repository(self):
        fixture = self.repository("literal", public.SNAPSHOT_MARKER)
        line = "pathlib.Path.home()"
        (fixture / "factory").mkdir()
        (fixture / "factory/demo.py").write_text(line + "\n")
        self.rules["reviewed_source_lines"] = {"factory/demo.py": [{
            "category": "internal hostname", "sha256": hashlib.sha256(line.encode()).hexdigest(),
            "reason": "Python expression, not a host."}]}
        self.git(fixture, "add", "--all")
        self.git(fixture, "commit", "-m", public.SNAPSHOT_MARKER)
        for repository, allowed in ((self.config["public"]["repository"], True), ("example-org/other", False)):
            url = "https://github.com/" + repository + ".git"
            findings = publication.check_push([url, "HEAD:refs/heads/main"], fixture,
                                             plan=[(url, "*", "HEAD", "refs/heads/main", "[new branch]")],
                                             advertised={url: {}})
            self.assertEqual(not findings, allowed, findings)
        head = self.git(fixture, "rev-parse", "HEAD")
        with patch.object(public.leaks, "scan", return_value=["synthetic secret finding"]):
            self.assertIn("synthetic secret finding", privacy_git.scan(
                fixture, [head], reviewed_source_lines=self.rules["reviewed_source_lines"]))

    def test_failed_push_preflight_cannot_produce_successful_plan(self):
        self.assertTrue(self.export()["ok"])
        with patch.object(publication, "check_push", return_value=["synthetic failure"]), \
                self.assertRaisesRegex(ValueError, "push preflight"):
            public.snapshot_plan(self.out, config=self.config)

    def test_main_parent_is_independent_of_checked_out_or_bare_head(self):
        fixture = self.repository("branches", public.SNAPSHOT_MARKER)
        first = self.git(fixture, "rev-parse", "HEAD")
        self.git(fixture, "commit", "--allow-empty", "-m", public.SNAPSHOT_MARKER)
        main = self.git(fixture, "rev-parse", "HEAD")
        self.git(fixture, "checkout", "-b", "preview", first)
        self.git(fixture, "commit", "--allow-empty", "-m", "Preview\n\n" + public.SNAPSHOT_MARKER)
        preview = self.git(fixture, "rev-parse", "HEAD")
        self.assertTrue(self.export()["ok"])
        self.assertEqual(public.snapshot_plan(self.out, clone=fixture, config=self.config)["parent"], main)
        self.assertEqual(self.git(fixture, "rev-parse", "HEAD"), preview)
        bare = self.root / "branches.git"
        self.git(self.root, "clone", "--bare", str(fixture), str(bare))
        for branch in ("preview", "unborn"):
            self.git(bare, "symbolic-ref", "HEAD", "refs/heads/" + branch)
            self.assertEqual(public.snapshot_plan(self.out, clone=bare, config=self.config)["parent"], main)

    def test_nonempty_destination_without_main_is_not_an_initial_snapshot(self):
        fixture = self.repository("missing-main", public.SNAPSHOT_MARKER)
        self.git(fixture, "branch", "-m", "preview")
        self.assertTrue(self.export()["ok"])
        with self.assertRaisesRegex(ValueError, "main"):
            public.snapshot_plan(self.out, clone=fixture, config=self.config)

    def test_empty_bare_destination_can_have_an_unborn_head(self):
        bare = self.root / "unborn.git"
        self.git(self.root, "init", "--bare", "--initial-branch=preview", str(bare))
        self.assertTrue(self.export()["ok"])
        result = public.snapshot_plan(self.out, clone=bare, config=self.config)
        self.assertIsNone(result["parent"])
        self.assertEqual(result["commits"], 1)

    def test_cli_publish_with_symlinked_temporary_ancestor(self):
        target = self.root / "real"
        (target / "tmp").mkdir(parents=True)
        alias = self.root / "alias"
        alias.symlink_to(target, target_is_directory=True)
        build = public.build
        output = io.StringIO()
        with patch.object(tempfile, "tempdir", str(alias / "tmp")), \
                patch.object(public, "build", side_effect=lambda out: build(
                    out, root=self.source, config=self.config, rules=self.rules)), redirect_stdout(output):
            self.assertEqual(cli.main(["public", "publish", "--dry-run"]), 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_worker_commit_uses_the_configured_clone_identity(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        rules = (root / "lines/oss/roles/_common.md").read_text()
        instruction = next(line for line in rules.splitlines() if line.startswith("- Commit only"))
        self.assertIn("`git commit`", instruction)
        self.assertNotIn("user.email=", instruction)
        fixture = self.repository("identity", "Base")
        base = self.git(fixture, "rev-parse", "HEAD")
        self.git(fixture, "update-ref", "refs/remotes/origin/main", base)
        self.git(fixture, "checkout", "-b", "fix")
        (fixture / "value.py").write_text("VALUE = 2\n")
        self.git(fixture, "add", "value.py")
        name, email = "Example Developer", "developer@example.org"
        # In-memory Git configuration models clone identity without writing any config or hook.
        env = {**public.git_environment(), "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "user.name",
               "GIT_CONFIG_VALUE_0": name, "GIT_CONFIG_KEY_1": "user.email", "GIT_CONFIG_VALUE_1": email}
        command = shlex.split(instruction.split("`")[1])
        subprocess.run([*command, "-m", "Correct the value"], cwd=fixture, env=env, capture_output=True, check=True)
        self.assertEqual(self.git(fixture, "show", "-s", "--format=%an <%ae>"), f"{name} <{email}>")
        itemdir = self.root / "item"
        itemdir.mkdir()
        (itemdir / "pr.md").write_text("# Correct the value\n\nFix the value for callers.\n")
        item = SimpleNamespace(clone=fixture, dir=itemdir, merge_base=lambda: base, file=lambda name: itemdir / name,
                               meta={"branch": "fix", "base": "main", "line": "oss", "repo": "example/project",
                                     "no_test_reason": "Synthetic identity-only probe"})
        with patch.dict(public.CONFIG, {"git_name": name, "git_email": email, "git_email_previous": [],
                                       "limits": DEFAULTS["limits"]}), \
                patch.object(slop.published, "on_pr_branch", return_value=()):
            result = slop.check(item)
        self.assertTrue(result["ok"], result)

    def test_public_metrics_documentation_matches_factory_controlled_direction(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        readme = (root / "factory/core/public-readme.txt").read_text()
        direction = (root / "docs/direction.md").read_text()
        self.assertIn("First-round quality", direction)
        self.assertIn("first-round", readme)
        self.assertNotIn("time\nto merge", readme)
        self.assertNotIn("Time to first maintainer response", direction)

    def test_scouting_fixture_is_synthetic_without_account_ids(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        cases = json.loads((root / "tests/fixtures/scouting-evidence.json").read_text())
        for case in cases:
            self.assertTrue(case["repo"].startswith("example-org/"))
            self.assertNotIn("fetched_at", case)
            for pr in case.get("linked", []):
                self.assertNotIn("id", pr["author"])

    def test_private_history_is_rejected_even_from_a_local_clone(self):
        private = self.repository("private-history", "Private operational records")
        self.assertTrue(self.export()["ok"])
        with self.assertRaisesRegex(ValueError, "non-snapshot history"):
            public.snapshot_plan(self.out, clone=private, config=self.config)


if __name__ == "__main__":
    unittest.main()
