import contextlib
import datetime as dt
import io
import itertools
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from support import setup_module
from factory import cli, util
from factory.core import clean, clones, nextaction
from factory.util import CONFIG, CommandError, Item, git, read_json, write_json


def setUpModule():
    setup_module(unittest)


class Clones(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name).resolve()
        self.upstream = self.root / "upstream.git"
        self.fork = self.root / "fork.git"
        self.seed = self.root / "seed"
        self.checkout = self.root / "repos" / "example__project"
        environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.use(patch.dict(os.environ, environment, clear=True))
        self.use(patch.dict(CONFIG, {
            "repos_dir": str(self.root / "repos"),
            "runs_dir": str(self.root / "runs"),
            "ledger_file": str(self.root / "ledger.json"),
            "limits": {"min_clone_free_gib": 0},
            "archive_dir": str(self.root / "archive"),
        }))
        self.use(patch.object(util, "ROOT", self.root))
        self.use(patch.object(clean, "ROOT", self.root))
        self.use(patch.object(util, "gh_json", side_effect=AssertionError("network not allowed")))
        # Bundle names carry a one-second timestamp. Give each archive its own second so that a fast machine
        # releasing the same checkout twice within one second does not trip the collision guard.
        ticks, start = itertools.count(), util.now()
        self.use(patch.object(clones, "now", side_effect=lambda: start + dt.timedelta(seconds=next(ticks))))
        write_json(self.root / "ledger.json", [])
        for repo in (self.upstream, self.fork):
            self.command("git", "init", "--bare", "-b", "main", str(repo))
            git(repo, "config", "uploadpack.allowFilter", "true")
        self.command("git", "init", "-b", "main", str(self.seed))
        self.identity(self.seed)
        (self.seed / "file.txt").write_text("base\n")
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-m", "base")
        git(self.seed, "remote", "add", "origin", self.upstream.as_uri())
        git(self.seed, "push", "origin", "main")
        self.command("git", "clone", self.upstream.as_uri(), str(self.checkout))
        self.identity(self.checkout)
        git(self.checkout, "remote", "add", "fork", self.fork.as_uri())
        git(self.checkout, "remote", "set-url", "--push", "fork", str(self.fork))
        self.base = git(self.checkout, "rev-parse", "HEAD")
        self.item = self.make_item("fix")

    def use(self, manager):
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value

    def command(self, *args):
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()

    def identity(self, repo):
        git(repo, "config", "user.name", CONFIG["git_name"])
        git(repo, "config", "user.email", CONFIG["git_email"])

    def make_item(self, slug, checkout=None, create_branch=True):
        checkout = checkout or self.checkout
        folder = self.root / "runs" / "2026-01-01" / slug
        folder.mkdir(parents=True)
        branch = "contrib/" + slug
        if create_branch:
            git(checkout, "branch", branch, "origin/main")
        write_json(folder / "item.json", {
            "repo": "example/project", "branch": branch, "base": "main",
            "clone": str(checkout), "status": "submitted", "schema": 2,
        })
        return Item(folder)

    def commit(self, item=None, push=False):
        item = item or self.item
        git(item.clone, "checkout", item.meta["branch"])
        (item.clone / "file.txt").write_text(item.meta["branch"] + "\n")
        git(item.clone, "add", "file.txt")
        git(item.clone, "commit", "-m", "fix behavior")
        if push:
            git(item.clone, "push", "fork", item.meta["branch"])
        return item.head_sha()

    def record(self, item=None):
        return read_json((item or self.item).meta_path).get("released")

    def missing_record(self, head):
        data = read_json(self.item.meta_path)
        data["released"] = {
            "at": "2026-01-01T00:00:00+00:00", "branch": data["branch"],
            "head": head, "base": self.base, "origin": self.upstream.as_uri(),
            "remotes": {"origin": {"url": self.upstream.as_uri()},
                        "fork": {"url": self.fork.as_uri()}},
        }
        write_json(self.item.meta_path, data)
        shutil.rmtree(self.checkout)
        return data["released"]

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["clone", *map(str, args)])
        return code, json.loads(output.getvalue())

    def test_pushed_fork_round_trip_and_recorded_head(self):
        head = self.commit(push=True)
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        self.assertFalse(self.checkout.exists())
        self.assertGreater(report["bytes"], 0)
        self.assertTrue(pathlib.Path(report["bundle"]).is_file())
        self.assertEqual(self.record()["bundle"], report["bundle"])
        self.assertEqual(report["items"], [str(self.item.dir)])
        record = self.record()
        self.assertEqual(record["head"], head)
        self.assertEqual(record["branch"], "contrib/fix")
        self.assertEqual(record["base"], self.base)
        self.assertEqual(record["origin"], self.upstream.as_uri())
        self.assertEqual(record["remotes"]["fork"], {"url": self.fork.as_uri(), "pushurl": str(self.fork)})
        self.assertNotIn("pushurl", record["remotes"]["origin"])
        self.assertEqual(self.item.head_sha(), head)
        self.assertEqual(self.item.merge_base(), self.base)
        with patch.object(clones, "run", wraps=util.run) as commands:
            result = clones.ensure(self.item)
        self.assertTrue(result["created"])
        self.assertTrue(any(c.args[0][:3] == ["git", "clone", "--filter=blob:none"] for c in commands.call_args_list))
        self.assertEqual(self.item.head_sha(), head)
        self.assertEqual(git(self.checkout, "branch", "--show-current"), "contrib/fix")
        self.assertEqual(git(self.checkout, "remote", "get-url", "--push", "fork"), str(self.fork))
        self.assertEqual(git(self.checkout, "config", "user.name"), CONFIG["git_name"])
        self.assertEqual(git(self.checkout, "config", "user.email"), CONFIG["git_email"])
        self.assertIsNone(self.record())

    def test_unpushed_bundle_restores_after_upstream_moves(self):
        head = self.commit()
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        bundle = pathlib.Path(report["bundle"])
        self.assertTrue(bundle.is_absolute())
        self.assertEqual(self.record()["bundle"], str(bundle))
        self.assertIn(head + " refs/heads/contrib/fix", self.command("git", "bundle", "list-heads", str(bundle)))
        (self.seed / "later.txt").write_text("later\n")
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-m", "advance base")
        git(self.seed, "push", "origin", "main")
        git(self.seed, "bundle", "verify", str(bundle))
        clones.ensure(self.item)
        self.assertEqual(self.item.head_sha(), head)
        self.assertEqual((self.checkout / "file.txt").read_text(), "contrib/fix\n")

    def test_profile_without_an_item_preserves_unpushed_main_in_bundle(self):
        profile = self.root / "repos" / f"{CONFIG['github_user']}__{CONFIG['github_user']}"
        shutil.move(self.checkout, profile)
        git(profile, "remote", "remove", "fork")
        (profile / "README.md").write_text("Unpushed profile prose.\n")
        git(profile, "add", "README.md")
        git(profile, "commit", "-m", "update profile")
        head = git(profile, "rev-parse", "HEAD")
        self.assertNotEqual(head, git(profile, "rev-parse", "origin/main"))

        report = clones.release(profile)
        self.assertTrue(report["released"], report)
        self.assertEqual(report["items"], [])
        self.assertFalse(profile.exists())
        bundle = pathlib.Path(report["bundle"])
        self.assertTrue(bundle.is_file())
        self.assertIn(head + " refs/heads/main", self.command("git", "bundle", "list-heads", str(bundle)))
        git(self.seed, "bundle", "verify", str(bundle))
        git(self.seed, "fetch", str(bundle), "refs/heads/main:refs/heads/recovered-profile")
        self.assertEqual(git(self.seed, "rev-parse", "recovered-profile"), head)
        self.assertEqual(git(self.seed, "show", "recovered-profile:README.md"), "Unpushed profile prose.")

    def test_tens_of_thousands_of_tags_fit_through_standard_input(self):
        head = self.commit()
        names = [f"refs/tags/packages/a-package-with-a-rather-long-name-{index:05d}@1.0.0-next.{index}" for index in range(40000)]
        self.assertGreater(sum(len(name) + 1 for name in names), 2 * 1024 * 1024)
        # One packed file instead of forty thousand loose ones keeps this test fast.
        git(self.checkout, "pack-refs", "--all")
        packed = self.checkout / ".git" / "packed-refs"
        existing = [line for line in packed.read_text().splitlines() if line and line[0] not in "#^"]
        rows = sorted(existing + [f"{self.base} {name}" for name in names], key=lambda row: row.split(" ", 1)[1])
        packed.write_text("# pack-refs with: peeled fully-peeled sorted \n" + "\n".join(rows) + "\n")
        self.assertEqual(git(self.checkout, "rev-parse", names[-1]), self.base)
        rows = [row for row in clones.status() if row["path"] == str(self.checkout)]
        self.assertEqual([(row["releasable"], row["why"]) for row in rows], [(True, [])])
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        self.assertEqual(self.command("git", "bundle", "list-heads", report["bundle"]).splitlines(),
                         [head + " refs/heads/contrib/fix"])
        clones.ensure(self.item)
        self.assertEqual(self.item.head_sha(), head)

    def test_branch_at_base_needs_no_bundle(self):
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        self.assertIsNone(report["bundle"])
        self.assertEqual(self.record()["head"], self.base)
        clones.ensure(self.item)
        self.assertEqual(self.item.head_sha(), self.base)

    def test_shared_clone_preserves_other_record_and_current_branch(self):
        first = self.commit(push=True)
        other = self.make_item("second")
        second = self.commit(other)
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        self.assertEqual(self.record()["bundle"], report["bundle"])
        self.assertEqual(self.record(other)["bundle"], report["bundle"])
        clones.ensure(self.item)
        self.assertIsNone(self.record())
        self.assertEqual(self.record(other)["head"], second)
        self.assertEqual(other.head_sha(), second)
        self.assertEqual(other.merge_base(), self.base)
        clones.ensure(other)
        self.assertEqual(other.head_sha(), second)
        self.assertEqual(self.item.head_sha(), first)
        self.assertEqual(git(self.checkout, "branch", "--show-current"), "contrib/fix")
        self.assertIsNone(self.record(other))

    def test_partly_restored_shared_clone_releases_again_and_keeps_sibling_records(self):
        self.commit()
        other = self.make_item("second")
        second = self.commit(other)
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        sibling = self.record(other)
        clones.ensure(Item(self.item.dir))
        (self.checkout / "more.txt").write_text("more\n")
        git(self.checkout, "add", "more.txt")
        git(self.checkout, "commit", "-m", "follow-up")
        advanced = Item(self.item.dir).head_sha()
        rows = [row for row in clones.status() if row["path"] == str(self.checkout)]
        self.assertEqual([(row["releasable"], row["why"]) for row in rows], [(True, [])])
        again = clones.release(self.checkout)
        self.assertTrue(again["released"], again)
        self.assertEqual(self.record(other), sibling)
        self.assertEqual(self.record()["head"], advanced)
        self.assertNotEqual(self.record()["bundle"], sibling["bundle"])
        clones.ensure(Item(other.dir))
        self.assertEqual(Item(other.dir).head_sha(), second)
        clones.ensure(Item(self.item.dir))
        self.assertEqual(Item(self.item.dir).head_sha(), advanced)

    def test_existing_branch_ignores_stale_record(self):
        head = self.commit()
        self.item.meta["released"] = {"head": "a" * 40, "base": "b" * 40}
        self.item.save()
        self.assertEqual(self.item.head_sha(), head)
        self.assertEqual(self.item.merge_base(), self.base)
        git(self.checkout, "checkout", "main")
        clones.ensure(self.item)
        self.assertIsNone(self.record())
        self.assertEqual(git(self.checkout, "branch", "--show-current"), "main")

    def test_branch_never_existed(self):
        git(self.checkout, "branch", "-D", self.item.meta["branch"])
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        self.assertIsNone(self.record()["head"])
        self.assertIsNone(self.record()["base"])
        with self.assertRaisesRegex(CommandError, "clone ensure --item"):
            self.item.merge_base()
        clones.ensure(self.item)
        self.assertFalse(git(self.checkout, "branch", "--list", self.item.meta["branch"]))
        self.assertIsNone(self.record())

    def test_released_only_accepts_an_object(self):
        self.assertIsNone(clones.released(self.item))
        self.item.meta["released"] = "not a record"
        self.item.save()
        self.assertIsNone(clones.released(self.item))

    def assert_kept(self, reason):
        before = self.item.meta_path.read_bytes()
        files = {p.relative_to(self.checkout): p.read_bytes() for p in self.checkout.rglob("*") if p.is_file()}
        report = clones.release(self.checkout)
        self.assertFalse(report["released"], report)
        self.assertIn(reason, " ".join(report["why"]).lower())
        self.assertTrue(self.checkout.is_dir())
        for name, content in files.items():
            self.assertTrue((self.checkout / name).is_file(), name)
            if name.parts[0] != ".git":
                self.assertEqual((self.checkout / name).read_bytes(), content)
        self.assertEqual(self.item.meta_path.read_bytes(), before)
        self.assertIsNone(self.record())
        self.assertEqual(report["bytes"], 0)

    def test_refuses_dirty_tracked_file(self):
        (self.checkout / "file.txt").write_text("work in progress\n")
        self.assert_kept("uncommitted")

    def test_refuses_untracked_file(self):
        (self.checkout / "new.txt").write_text("work in progress\n")
        self.assert_kept("uncommitted")

    def test_refuses_stash(self):
        (self.checkout / "file.txt").write_text("stashed\n")
        git(self.checkout, "stash", "push")
        self.assert_kept("stash")

    def test_refuses_linked_worktree_on_primary_clone(self):
        worktree = self.root / "workspace" / "run" / "fix"
        git(self.checkout, "worktree", "add", str(worktree), self.item.meta["branch"])
        self.assert_kept("worktree")
        self.assertTrue((worktree / "file.txt").is_file())

    def test_refuses_failed_fetch(self):
        shutil.rmtree(self.fork)
        self.assert_kept("fetch")

    def test_refuses_if_writing_a_record_fails(self):
        self.commit()
        with patch.object(clones, "write_json", side_effect=OSError("disk full")):
            self.assert_kept("disk full")

    def test_failed_bundle_verification_removes_bundle(self):
        self.commit()
        original = clones.git

        def broken(repo, *args, **kwargs):
            if args[:2] == ("bundle", "verify"):
                raise CommandError("bundle verify failed")
            return original(repo, *args, **kwargs)

        with patch.object(clones, "git", side_effect=broken):
            self.assert_kept("bundle")
        self.assertEqual(list((self.root / "archive").glob("*.bundle")), [])

    def test_bundle_must_list_every_branch_tip(self):
        self.commit()
        original = clones.git

        def missing(repo, *args, **kwargs):
            if args[:2] == ("bundle", "list-heads"):
                return ""
            return original(repo, *args, **kwargs)

        with patch.object(clones, "git", side_effect=missing):
            self.assert_kept("bundle")
        self.assertEqual(list((self.root / "archive").glob("*.bundle")), [])

    def test_missing_head_fails_without_creating_branch(self):
        head = "a" * 40
        record = self.missing_record(head)
        with self.assertRaises(CommandError) as error:
            clones.ensure(self.item)
        self.assertIn(str(self.item.dir), str(error.exception))
        self.assertIn(head, str(error.exception))
        self.assertIn("fork", str(error.exception))
        self.assertEqual(self.record(), record)
        self.assertFalse(git(self.checkout, "branch", "--list", "contrib/fix"))

    def test_moved_remote_branch_is_not_used(self):
        old = self.commit(push=True)
        clones.release(self.checkout)
        git(self.seed, "fetch", self.fork.as_uri(), "contrib/fix")
        git(self.seed, "checkout", "-b", "contrib/fix", "FETCH_HEAD")
        (self.seed / "file.txt").write_text("moved\n")
        git(self.seed, "commit", "-am", "advance fork")
        git(self.seed, "push", self.fork.as_uri(), "contrib/fix")
        clones.ensure(self.item)
        self.assertEqual(self.item.head_sha(), old)
        self.assertIsNone(self.record())

    def test_head_fetch_when_named_branch_was_deleted(self):
        head = self.commit(push=True)
        clones.release(self.checkout)
        git(self.fork, "update-ref", "-d", "refs/heads/contrib/fix")
        git(self.fork, "config", "uploadpack.allowAnySHA1InWant", "true")
        clones.ensure(self.item)
        self.assertEqual(self.item.head_sha(), head)

    def test_dry_run_changes_nothing_and_skips_fetch(self):
        self.commit()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch.object(clones, "git", wraps=git) as commands:
            report = clones.release(self.checkout, dry_run=True)
        self.assertFalse(report["released"])
        self.assertTrue(report["would_release"])
        self.assertIn("would fetch", report["why"])
        self.assertEqual(report["bytes"], 0)
        self.assertFalse(any(c.args[1:2] == ("fetch",) for c in commands.call_args_list))
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_release_linked_checkout_and_restore(self):
        worktree = self.root / "workspace" / "run" / "second"
        other = self.make_item("second", create_branch=False)
        git(self.checkout, "worktree", "add", "-b", other.meta["branch"], str(worktree), "origin/main")
        other.meta["clone"] = str(worktree)
        other.save()
        head = self.commit(other)
        report = clones.release(worktree)
        self.assertTrue(report["released"], report)
        self.assertFalse(worktree.exists())
        self.assertTrue(self.checkout.exists())
        self.assertNotIn(str(worktree), git(self.checkout, "worktree", "list", "--porcelain"))
        clones.ensure(other)
        self.assertEqual(other.head_sha(), head)

    def test_low_free_space_refuses_before_cloning(self):
        clones.release(self.checkout)
        self.use(patch.dict(CONFIG, {"limits": {"min_clone_free_gib": 2}}))
        usage = shutil._ntuple_diskusage(4 * 2**30, 3 * 2**30, 2**30)
        with patch.object(shutil, "disk_usage", return_value=usage):
            with self.assertRaisesRegex(CommandError, "2.*GiB"):
                clones.ensure(self.item)
        self.assertFalse(self.checkout.exists())
        self.assertIsNotNone(self.record())

    def test_ensure_repo_uses_default_url_and_blobless_clone(self):
        shutil.rmtree(self.checkout)
        original = clones.run

        def local_clone(args, **kwargs):
            if args[:2] == ["git", "clone"]:
                self.assertEqual(args[2:4], ["--filter=blob:none", "https://github.com/example/project.git"])
                args = [*args[:3], self.upstream.as_uri(), *args[4:]]
            return original(args, **kwargs)

        with patch.object(clones, "run", side_effect=local_clone):
            result = clones.ensure_repo("example/project")
        self.assertEqual(result, self.checkout)
        with patch.object(clones, "run", side_effect=AssertionError("already cloned")):
            self.assertEqual(clones.ensure_repo("example/project"), self.checkout)

    def test_status_reports_items_and_ledger_status_without_writing(self):
        write_json(self.root / "ledger.json", [{"item": str(self.item.dir), "status": "merged"}])
        rows = clones.status()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], str(self.checkout))
        self.assertGreater(rows[0]["size"], 0)
        self.assertEqual(rows[0]["items"], [{"item": str(self.item.dir), "status": "merged"}])
        self.assertTrue(rows[0]["releasable"])
        (self.checkout / "untracked").write_text("keep\n")
        row = clones.status()[0]
        self.assertFalse(row["releasable"])
        self.assertIn("uncommitted", " ".join(row["why"]))

    def test_release_idle_keeps_active_items(self):
        result = clones.release_idle({self.item.dir})
        self.assertEqual(result, {"reports": [], "bytes": 0})
        self.assertTrue(self.checkout.exists())
        result = clones.release_idle(set())
        self.assertTrue(result["reports"][0]["released"])
        self.assertGreater(result["bytes"], 0)

    def test_release_idle_keeps_profile_and_reports_other_checkouts(self):
        user = CONFIG["github_user"]
        profile = util.clone_dir(f"{user}/{user}")
        dirty = self.root / "repos" / "dirty"
        for checkout in (profile, dirty):
            self.command("git", "clone", self.upstream.as_uri(), str(checkout))
        (dirty / "keep.txt").write_text("work in progress\n")
        before = {p.relative_to(profile): p.read_bytes() for p in profile.rglob("*") if p.is_file()}
        alias = self.root / "repos-alias"
        alias.symlink_to(profile.parent, target_is_directory=True)
        with patch.dict(CONFIG, {"repos_dir": str(alias)}):
            for dry_run in (True, False):
                with self.subTest(dry_run=dry_run):
                    result = clones.release_idle(set(), dry_run=dry_run)
                    self.assertEqual({row["checkout"]: row["status"] for row in result["reports"]},
                                     {str(dirty): "protected", str(self.checkout): "would_release" if dry_run else "released"})
                    self.assertEqual(before, {p.relative_to(profile): p.read_bytes() for p in profile.rglob("*") if p.is_file()})
                    self.assertTrue(profile.is_dir())
                    self.assertEqual(self.checkout.exists(), dry_run)
                    self.assertEqual((dirty / "keep.txt").read_text(), "work in progress\n")

    def test_idle_cli_succeeds_with_protected_clones_and_reports_each_result(self):
        dirty = self.root / "repos" / "dirty"
        self.command("git", "clone", self.upstream.as_uri(), str(dirty))
        (dirty / "keep.txt").write_text("work in progress\n")
        with patch.object(nextaction, "code_items", return_value=set()):
            code, result = self.invoke("release", "--idle", "--dry-run")
            self.assertEqual(code, 0, result)
            self.assertEqual({r["checkout"]: r["status"] for r in result["reports"]},
                             {str(dirty): "protected", str(self.checkout): "would_release"})
            self.assertTrue(self.checkout.exists())
            code, result = self.invoke("release", "--idle")
            self.assertEqual(code, 0, result)
            self.assertEqual({r["checkout"]: r["status"] for r in result["reports"]},
                             {str(dirty): "protected", str(self.checkout): "released"})
            self.assertGreater(result["bytes"], 0)
            self.assertFalse(self.checkout.exists())
            self.assertTrue((dirty / "keep.txt").exists())
            code, result = self.invoke("release", "--idle")
            self.assertEqual(code, 0, result)
            self.assertEqual([r["status"] for r in result["reports"]], ["protected"])
            self.assertIn("uncommitted", " ".join(result["reports"][0]["why"]))

    def test_idle_cli_fails_on_unexpected_fetch_and_verification_errors(self):
        with patch.object(nextaction, "code_items", return_value=set()):
            for check in ("_dirty", "_submodule_reasons"):
                with self.subTest(check=check), patch.object(clones, check, side_effect=CommandError("read failed")):
                    code, result = self.invoke("release", "--idle")
                    self.assertEqual(code, 1, result)
                    self.assertEqual(result["reports"][0]["status"], "error")
                    self.assertIn("read failed", " ".join(result["reports"][0]["why"]))
                    self.assertTrue(self.checkout.exists())
            shutil.rmtree(self.fork)
            code, result = self.invoke("release", "--idle")
            self.assertEqual(code, 1, result)
            self.assertEqual(result["reports"][0]["status"], "error")
            self.assertIn("fetch", " ".join(result["reports"][0]["why"]))
            self.assertTrue(self.checkout.exists())

    def test_release_idle_removes_worktrees_before_primary(self):
        worktree = self.root / "workspace" / "run" / "other"
        git(self.checkout, "worktree", "add", "-b", "contrib/other", str(worktree), "origin/main")
        result = clones.release_idle(set())
        self.assertEqual([r["checkout"] for r in result["reports"]], [str(worktree), str(self.checkout)])
        self.assertTrue(all(r["released"] for r in result["reports"]), result)

    def test_cli_status_release_ensure_json_and_exit_codes(self):
        code, rows = self.invoke("status")
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["path"], str(self.checkout))
        code, report = self.invoke("release", "--item", self.item.dir, "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(report["would_release"])
        (self.checkout / "untracked").write_text("keep\n")
        code, report = self.invoke("release", "--item", self.item.dir)
        self.assertEqual(code, 1)
        self.assertFalse(report["released"])
        (self.checkout / "untracked").unlink()
        code, report = self.invoke("release", "--item", self.item.dir)
        self.assertEqual(code, 0)
        self.assertTrue(report["released"])
        code, report = self.invoke("ensure", "--item", self.item.dir)
        self.assertEqual(code, 0)
        self.assertEqual(report["head"], self.base)
        code, report = self.invoke("ensure", "--repo", "example/project")
        self.assertEqual(code, 0)
        self.assertEqual(report["checkout"], str(self.checkout))
        self.missing_record("a" * 40)
        code, report = self.invoke("ensure", "--item", self.item.dir)
        self.assertEqual(code, 1)
        self.assertIn("a" * 40, report["error"])

    def test_default_branch_is_not_created_twice_on_ensure(self):
        git(self.checkout, "branch", "-D", self.item.meta["branch"])
        self.item.meta["branch"] = "main"
        self.item.save()
        self.assertTrue(clones.release(self.checkout)["released"])
        clones.ensure(self.item)
        self.assertEqual(self.item.head_sha(), self.base)
        self.assertEqual(git(self.checkout, "branch", "--show-current"), "main")

    def test_tag_with_same_name_does_not_hide_item_branch(self):
        head = self.commit(push=True)
        git(self.checkout, "tag", "contrib/fix", self.base)
        git(self.checkout, "push", "origin", "refs/tags/contrib/fix")
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        self.assertEqual(self.record()["head"], head)
        clones.ensure(self.item)
        self.assertEqual(git(self.checkout, "rev-parse", "refs/heads/contrib/fix"), head)

    def test_idle_release_preserves_headless_and_protected_item_bytes(self):
        other = self.make_item("old-scout", create_branch=False)
        self.assertTrue(clones.release(self.checkout)["released"])
        before = other.meta_path.read_bytes()
        self.assertIsNone(self.record(other)["head"])
        clones.ensure(self.item)
        protected = self.item.meta_path.read_bytes()
        self.assertEqual(clones.release_idle({self.item.dir})["reports"], [])
        self.assertEqual(self.item.meta_path.read_bytes(), protected)
        self.assertEqual(other.meta_path.read_bytes(), before)
        with patch.object(clones, "iso", return_value="2099-01-01T00:00:00Z"):
            report = clones.release_idle(set())
        self.assertTrue(report["reports"][0]["released"], report)
        self.assertEqual(other.meta_path.read_bytes(), before)
        self.assertNotEqual(self.item.meta_path.read_bytes(), protected)

    def test_release_never_overwrites_the_record_of_a_branch_that_is_still_released(self):
        head = self.commit(push=True)
        other = self.make_item("second")
        second = self.commit(other)
        self.assertTrue(clones.release(self.checkout)["released"])
        clones.ensure(self.item)
        original = self.record(other)
        row = clones.status()[0]
        self.assertEqual((row["releasable"], row["why"]), (True, []))
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        self.assertEqual(self.record(other), original)
        self.assertEqual(self.record()["head"], head)
        clones.ensure(Item(other.dir))
        self.assertEqual(Item(other.dir).head_sha(), second)

    def test_archive_cannot_live_in_a_managed_checkout(self):
        worktree = self.root / "workspace" / "run" / "other"
        other = self.make_item("other", create_branch=False)
        git(self.checkout, "worktree", "add", "-b", other.meta["branch"], str(worktree), "origin/main")
        other.meta["clone"] = str(worktree)
        other.save()
        self.commit(other)
        with patch.dict(CONFIG, {"archive_dir": str(self.checkout / "archive")}):
            report = clones.release(worktree)
        self.assertFalse(report["released"], report)
        self.assertTrue(worktree.exists())
        self.assertIsNone(self.record(other))

    def test_archive_defaults_next_to_repos_directory(self):
        self.commit()
        config = dict(CONFIG)
        del config["archive_dir"]
        with patch.dict(CONFIG, config, clear=True):
            report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        self.assertEqual(pathlib.Path(report["bundle"]).parent, self.checkout.parent.parent / "archive")

    def test_refuses_a_detached_commit_with_no_branch(self):
        git(self.checkout, "checkout", "--detach")
        git(self.checkout, "commit", "--allow-empty", "-m", "detached work")
        self.assert_kept("detached")

    def test_multiple_unpushed_branches_share_a_verified_bundle(self):
        first = self.commit()
        other = self.make_item("second")
        second = self.commit(other)
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        self.assertEqual(self.record()["bundle"], self.record(other)["bundle"])
        heads = self.command("git", "bundle", "list-heads", report["bundle"])
        self.assertIn(first + " refs/heads/contrib/fix", heads)
        self.assertIn(second + " refs/heads/contrib/second", heads)
        clones.ensure(other)
        clones.ensure(self.item)
        self.assertEqual(self.item.head_sha(), first)
        self.assertEqual(other.head_sha(), second)
        self.assertEqual(git(self.checkout, "branch", "--show-current"), "contrib/second")

    def test_release_catches_work_written_while_bundle_is_created(self):
        self.commit()
        original = clones.git

        def edit_during_verify(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[:2] == ("bundle", "verify"):
                (self.checkout / "file.txt").write_text("new work\n")
            return result

        with patch.object(clones, "git", side_effect=edit_during_verify):
            report = clones.release(self.checkout)
        self.assertFalse(report["released"], report)
        self.assertEqual((self.checkout / "file.txt").read_text(), "new work\n")

    def test_cli_idle_protection_succeeds_but_a_missing_item_is_an_error(self):
        (self.checkout / "untracked").write_text("keep\n")
        with patch.object(nextaction, "code_items", return_value=set(), create=True):
            code, report = self.invoke("release", "--idle", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(report["reports"][0]["status"], "protected")
        self.assertFalse(report["reports"][0]["would_release"])
        code, report = self.invoke("ensure", "--item", self.root / "missing-item")
        self.assertEqual(code, 1)
        self.assertIn("not found", report["error"])

    def test_default_free_space_limit_also_applies_before_an_item_exists(self):
        shutil.rmtree(self.checkout)
        usage = shutil._ntuple_diskusage(2**30, 2**29, 2**29)
        with patch.dict(CONFIG, {"limits": {}}), patch.object(shutil, "disk_usage", return_value=usage):
            with self.assertRaisesRegex(CommandError, "at least 1 GiB"):
                clones.ensure_repo("example/project")
        self.assertFalse(self.checkout.exists())

    def test_ensure_keeps_non_checkout_contents_and_the_release_record(self):
        self.missing_record(self.base)
        self.checkout.mkdir()
        (self.checkout / "precious.txt").write_text("keep\n")
        record = self.record()
        with self.assertRaisesRegex(CommandError, "not a checkout"):
            clones.ensure(self.item)
        self.assertEqual(self.record(), record)
        self.assertEqual((self.checkout / "precious.txt").read_text(), "keep\n")

    def test_partial_record_write_does_not_delete_the_checkout(self):
        first = self.commit(push=True)
        other = self.make_item("second")
        second = self.commit(other)

        def fail_second(path, data):
            if path == other.meta_path:
                raise OSError("disk full")
            write_json(path, data)

        with patch.object(clones, "write_json", side_effect=fail_second):
            report = clones.release(self.checkout)
        self.assertFalse(report["released"], report)
        self.assertEqual(self.item.head_sha(), first)
        self.assertEqual(other.head_sha(), second)
        self.assertEqual(self.record()["head"], first)
        self.assertIsNone(self.record(other))
        self.assertTrue(pathlib.Path(report["bundle"]).is_file())

    def test_ensure_item_without_a_record_uses_default_origin_without_inventing_a_branch(self):
        shutil.rmtree(self.checkout)
        original = clones.run

        def local_clone(args, **kwargs):
            if args[:2] == ["git", "clone"]:
                self.assertEqual(args[3], "https://github.com/example/project.git")
                args = [*args[:3], self.upstream.as_uri(), *args[4:]]
            return original(args, **kwargs)

        with patch.object(clones, "run", side_effect=local_clone):
            result = clones.ensure(self.item)
        self.assertTrue(result["created"])
        self.assertIsNone(result["head"])
        self.assertFalse(git(self.checkout, "branch", "--list", "contrib/fix"))
        self.assertEqual(git(self.checkout, "branch", "--show-current"), "main")

    def test_nested_run_items_receive_release_records(self):
        nested = self.item.dir.parent / "sweep" / self.item.dir.name
        nested.parent.mkdir()
        self.item.dir.rename(nested)
        item = Item(nested)
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        self.assertEqual(report["items"], [str(nested)])
        self.assertEqual(clones.released(item)["head"], self.base)

    def prune(self, repo):
        git(repo, "reflog", "expire", "--expire=now", "--all")
        git(repo, "gc", "--prune=now")

    def discard_fork_tip(self, rewind):
        ref = "refs/heads/" + self.item.meta["branch"]
        if rewind:
            git(self.fork, "update-ref", ref, self.base)
        else:
            git(self.fork, "update-ref", "-d", ref)
        self.prune(self.fork)

    def test_deleted_fork_after_release_restores_from_bundle(self):
        head = self.commit(push=True)
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        self.discard_fork_tip(rewind=False)
        clones.ensure(self.item)
        self.assertEqual(self.item.head_sha(), head)
        self.assertEqual(self.record(), None)

    def test_rewound_fork_after_release_restores_from_bundle(self):
        head = self.commit(push=True)
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        self.discard_fork_tip(rewind=True)
        clones.ensure(self.item)
        self.assertEqual(self.item.head_sha(), head)

    def test_rewound_fork_before_release_does_not_block_bundle(self):
        head = self.commit(push=True)
        self.discard_fork_tip(rewind=True)
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        clones.ensure(self.item)
        self.assertEqual(self.item.head_sha(), head)

    def test_local_ahead_of_fork_bundle_needs_only_origin_history(self):
        pushed = self.commit(push=True)
        git(self.checkout, "commit", "--allow-empty", "-m", "local work")
        head = self.item.head_sha()
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        git(self.seed, "bundle", "verify", report["bundle"])
        self.discard_fork_tip(rewind=True)
        clones.ensure(self.item)
        self.assertEqual(self.item.head_sha(), head)
        git(self.checkout, "cat-file", "-e", pushed + "^{commit}")

    def test_older_fork_does_not_block_local_ahead_restore(self):
        self.commit(push=True)
        git(self.checkout, "commit", "--allow-empty", "-m", "local work")
        head = self.item.head_sha()
        self.assertTrue(clones.release(self.checkout)["released"])
        clones.ensure(self.item)
        self.assertEqual(self.item.head_sha(), head)

    def test_mismatched_remote_continues_to_another_remote(self):
        head = self.commit(push=True)
        mirror = self.root / "mirror.git"
        self.command("git", "clone", "--bare", self.fork.as_uri(), str(mirror))
        git(self.checkout, "remote", "add", "mirror", mirror.as_uri())
        self.assertTrue(clones.release(self.checkout)["released"])
        self.discard_fork_tip(rewind=True)
        original = clones.run
        sources = []

        def observe(args, **kwargs):
            if args[3:4] == ["fetch"]:
                sources.append(args[4:])
            return original(args, **kwargs)

        with patch.object(clones, "run", side_effect=observe):
            clones.ensure(self.item)
        self.assertEqual(self.item.head_sha(), head)
        self.assertEqual(sources, [["fork", "refs/heads/contrib/fix"],
                                   ["mirror", "refs/heads/contrib/fix"]])

    def test_exhausted_sources_report_every_result(self):
        self.commit(push=True)
        head = "a" * 40
        record = self.missing_record(head)
        with self.assertRaises(CommandError) as error:
            clones.ensure(self.item)
        message = str(error.exception)
        for remote in ("origin", "fork"):
            self.assertIn(remote + " refs/heads/contrib/fix", message)
            self.assertIn(remote + " " + head, message)
        self.assertIn("returned", message)
        self.assertIn("failed", message)
        self.assertEqual(self.record(), record)
        self.assertFalse(git(self.checkout, "branch", "--list", "contrib/fix"))

    def preserve_nonbranch_ref(self, ref, annotated=False):
        head = self.commit()
        if annotated:
            git(self.checkout, "tag", "-a", ref.removeprefix("refs/tags/"), "-m", "saved work", head)
        else:
            git(self.checkout, "update-ref", ref, head)
        tip = git(self.checkout, "rev-parse", ref)
        git(self.checkout, "checkout", "main")
        git(self.checkout, "branch", "-f", "contrib/fix", self.base)
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        self.assertIsNotNone(report["bundle"])
        self.assertNotIn("bundle", self.record())
        heads = self.command("git", "bundle", "list-heads", report["bundle"])
        self.assertIn(tip + " " + ref, heads)
        clones.ensure(self.item)
        git(self.checkout, "fetch", report["bundle"], ref + ":" + ref)
        self.assertEqual(git(self.checkout, "rev-parse", ref), tip)
        self.assertEqual(git(self.checkout, "rev-parse", ref + "^{commit}"), head)

    def test_local_tag_only_commit_is_bundled(self):
        self.preserve_nonbranch_ref("refs/tags/saved-work")

    def test_annotated_tag_only_commit_preserves_tag_object(self):
        self.preserve_nonbranch_ref("refs/tags/saved-work", annotated=True)

    def test_other_local_ref_only_commit_is_bundled(self):
        self.preserve_nonbranch_ref("refs/keep/saved-work")

    def test_bundle_must_verify_tag_tip(self):
        head = self.commit()
        git(self.checkout, "tag", "saved-work", head)
        original = clones.git

        def missing_tag(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[:2] == ("bundle", "list-heads"):
                return "\n".join(line for line in result.splitlines() if not line.endswith("refs/tags/saved-work"))
            return result

        with patch.object(clones, "git", side_effect=missing_tag):
            self.assert_kept("saved-work")

    def test_tag_changed_during_bundle_creation_keeps_checkout(self):
        head = self.commit()
        git(self.checkout, "tag", "saved-work", head)
        original = clones.git

        def move_tag(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[:2] == ("bundle", "list-heads"):
                git(self.checkout, "tag", "-f", "saved-work", self.base)
            return result

        with patch.object(clones, "git", side_effect=move_tag):
            self.assert_kept("changed")

    def test_status_config_cannot_hide_untracked_files(self):
        git(self.checkout, "config", "status.showUntrackedFiles", "no")
        (self.checkout / "precious.txt").write_text("keep\n")
        self.assertFalse(git(self.checkout, "status", "--porcelain"))
        self.assert_kept("uncommitted")

    def test_global_status_config_cannot_hide_untracked_files(self):
        config = self.root / "gitconfig"
        config.write_text("[status]\n\tshowUntrackedFiles = no\n")
        (self.checkout / "precious.txt").write_text("keep\n")
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}):
            self.assertFalse(git(self.checkout, "status", "--porcelain"))
            self.assert_kept("uncommitted")

    def add_submodule(self, parent=None, path="modules/child"):
        if parent is None:
            parent = self.checkout
            git(parent, "checkout", self.item.meta["branch"])
        git(parent, "-c", "protocol.file.allow=always", "submodule", "add", self.upstream.as_uri(), path)
        git(parent, "commit", "-m", "add submodule")
        submodule = parent / path
        self.identity(submodule)
        return submodule

    def test_submodule_unpushed_commit_keeps_checkout(self):
        child = self.add_submodule()
        git(child, "commit", "--allow-empty", "-m", "child work")
        git(self.checkout, "add", "modules/child")
        git(self.checkout, "commit", "-m", "record child work")
        self.assertFalse(git(self.checkout, "status", "--porcelain"))
        self.assert_kept("submodule modules/child")

    def test_submodule_other_branch_unpushed_commit_keeps_checkout(self):
        child = self.add_submodule()
        git(child, "checkout", "-b", "local-work")
        git(child, "commit", "--allow-empty", "-m", "child branch work")
        git(child, "checkout", "main")
        self.assert_kept("submodule modules/child")

    def test_submodule_detached_unpushed_commit_keeps_checkout(self):
        child = self.add_submodule()
        git(child, "checkout", "--detach")
        git(child, "commit", "--allow-empty", "-m", "child detached work")
        git(self.checkout, "add", "modules/child")
        git(self.checkout, "commit", "-m", "record child work")
        self.assert_kept("submodule modules/child")

    def test_submodule_stash_keeps_checkout(self):
        child = self.add_submodule()
        (child / "file.txt").write_text("child stash\n")
        git(child, "stash", "push")
        self.assertFalse(git(self.checkout, "status", "--porcelain"))
        self.assert_kept("submodule modules/child")

    def test_submodule_ignore_config_cannot_hide_untracked_work(self):
        child = self.add_submodule()
        git(self.checkout, "config", "submodule.modules/child.ignore", "all")
        git(child, "config", "status.showUntrackedFiles", "no")
        (child / "precious.txt").write_text("child work\n")
        self.assert_kept("submodule modules/child")

    def test_submodule_ignore_config_cannot_hide_tracked_work(self):
        child = self.add_submodule()
        git(self.checkout, "config", "diff.ignoreSubmodules", "all")
        (child / "file.txt").write_text("child work\n")
        self.assert_kept("submodule modules/child")

    def test_nested_submodule_stash_keeps_checkout(self):
        child = self.add_submodule()
        nested = self.add_submodule(child, "nested")
        git(self.checkout, "add", "modules/child")
        git(self.checkout, "commit", "-m", "record nested module")
        (nested / "file.txt").write_text("nested work\n")
        git(nested, "stash", "push")
        self.assert_kept("submodule modules/child/nested")

    def test_clean_remote_submodule_can_be_released(self):
        child = self.add_submodule()
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        clones.ensure(self.item)
        self.assertIsNone(self.record())
        git(self.checkout, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive")
        self.assertEqual(git(child, "rev-parse", "HEAD"), self.base)

    def test_ignored_files_remain_disposable(self):
        (self.checkout / ".gitignore").write_text(".env\n")
        git(self.checkout, "add", ".gitignore")
        git(self.checkout, "commit", "-m", "ignore environment file")
        (self.checkout / ".env").write_text("DISPOSABLE=value\n")
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        clones.ensure(self.item)
        self.assertFalse((self.checkout / ".env").exists())

    def test_rewritten_origin_reports_missing_bundle_prerequisites(self):
        self.commit()
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)
        record = self.record()
        git(self.seed, "checkout", "--orphan", "replacement")
        git(self.seed, "commit", "-am", "replacement history")
        git(self.seed, "push", "--force", "origin", "replacement:main")
        self.prune(self.upstream)
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                with self.assertRaisesRegex(CommandError, "prerequisite commits are missing from origin") as error:
                    clones.ensure(self.item)
                self.assertIn(report["bundle"], str(error.exception))
                self.assertEqual(self.record(), record)

    def advance_main(self):
        (self.seed / "later.txt").write_text("later\n")
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-m", "advance main")
        git(self.seed, "push", "origin", "main")
        return git(self.seed, "rev-parse", "HEAD")

    def test_failed_ensure_retry_never_accepts_moved_default_branch(self):
        self.item.meta["branch"] = "main"
        self.item.save()
        self.assertTrue(clones.release(self.checkout)["released"])
        record = self.record()
        moved = self.advance_main()
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                with self.assertRaisesRegex(CommandError, "move the incomplete clone aside"):
                    clones.ensure(self.item)
                self.assertEqual(self.record(), record)
                self.assertEqual(git(self.checkout, "rev-parse", "refs/heads/main"), moved)
                self.assertTrue((self.checkout / ".git" / "factory-restoring").is_file())

    def test_failed_ensure_resumes_and_removes_marker_on_success(self):
        head = "a" * 40
        self.missing_record(head)
        with self.assertRaises(CommandError):
            clones.ensure(self.item)
        marker = self.checkout / ".git" / "factory-restoring"
        self.assertTrue(marker.is_file())
        data = read_json(self.item.meta_path)
        data["released"]["head"] = self.base
        write_json(self.item.meta_path, data)
        clones.ensure(self.item)
        self.assertEqual(self.item.head_sha(), self.base)
        self.assertEqual(git(self.checkout, "branch", "--show-current"), "contrib/fix")
        self.assertFalse(marker.exists())
        self.assertIsNone(self.record())

    def test_failed_clone_configuration_still_marks_incomplete_checkout(self):
        self.item.meta["branch"] = "main"
        self.item.save()
        self.assertTrue(clones.release(self.checkout)["released"])
        record = self.record()
        moved = self.advance_main()
        original = clones.git

        def fail_config(repo, *args, **kwargs):
            if args[:2] == ("config", "user.name"):
                raise CommandError("cannot configure clone")
            return original(repo, *args, **kwargs)

        with patch.object(clones, "git", side_effect=fail_config):
            with self.assertRaisesRegex(CommandError, "cannot configure"):
                clones.ensure(self.item)
        with self.assertRaisesRegex(CommandError, "move the incomplete clone aside"):
            clones.ensure(self.item)
        self.assertEqual(self.record(), record)
        self.assertEqual(git(self.checkout, "rev-parse", "main"), moved)

    def test_release_refuses_an_incomplete_restore(self):
        self.item.meta["branch"] = "main"
        self.item.save()
        self.assertTrue(clones.release(self.checkout)["released"])
        record = self.record()
        self.advance_main()
        with self.assertRaises(CommandError):
            clones.ensure(self.item)
        report = clones.release(self.checkout)
        self.assertFalse(report["released"], report)
        self.assertIn("incomplete", " ".join(report["why"]))
        self.assertEqual(self.record(), record)
        self.assertTrue(self.checkout.is_dir())

    def test_restore_rechecks_tip_before_clearing_record(self):
        head = self.commit()
        self.assertTrue(clones.release(self.checkout)["released"])
        record = self.record()
        original = clones.git

        def move_after_checkout(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args == ("checkout", "contrib/fix"):
                original(repo, "update-ref", "refs/heads/contrib/fix", self.base)
            return result

        with patch.object(clones, "git", side_effect=move_after_checkout):
            with self.assertRaisesRegex(CommandError, head):
                clones.ensure(self.item)
        self.assertEqual(self.record(), record)
        self.assertTrue((self.checkout / ".git" / "factory-restoring").is_file())

    def drop_record(self):
        data = read_json(self.item.meta_path)
        data.pop("released")
        write_json(self.item.meta_path, data)
        self.item.meta = data

    def interrupted_restore(self):
        """Release an unpushed commit, hide its bundle and let ensure fail after it cloned."""
        head = self.commit()
        self.assertTrue(clones.release(self.checkout)["released"])
        bundle = pathlib.Path(self.record()["bundle"])
        hidden = bundle.with_suffix(".away")
        bundle.rename(hidden)
        with self.assertRaises(CommandError):
            clones.ensure(self.item)
        marker = self.checkout / ".git" / "factory-restoring"
        self.assertEqual(json.loads(marker.read_text()),
                         {"item": str(self.item.dir), "branch": "contrib/fix", "head": head, "released": True})
        return head, bundle, hidden, marker

    def test_unfinished_restore_without_a_record_never_reports_success(self):
        head, _, _, marker = self.interrupted_restore()
        self.drop_record()
        for _ in range(2):
            with self.assertRaisesRegex(CommandError, "no release record"):
                clones.ensure(self.item)
        self.assertTrue(marker.is_file())
        self.assertNotIn("contrib/fix", git(self.checkout, "branch", "--format=%(refname:short)").splitlines())

    def test_unfinished_restore_without_a_record_accepts_only_the_marked_tip(self):
        head, bundle, hidden, marker = self.interrupted_restore()
        self.drop_record()
        git(self.checkout, "branch", "contrib/fix", self.base)
        with self.assertRaisesRegex(CommandError, "expected head " + head):
            clones.ensure(self.item)
        self.assertTrue(marker.is_file())
        git(self.checkout, "branch", "-D", "contrib/fix")
        hidden.rename(bundle)
        git(self.checkout, "fetch", str(bundle), "refs/heads/contrib/fix:refs/heads/contrib/fix")
        result = clones.ensure(self.item)
        self.assertEqual(result["head"], head)
        self.assertFalse(marker.exists())

    def test_unfinished_restore_for_another_item_is_not_taken_over(self):
        other = self.make_item("other")
        self.interrupted_restore()
        record = self.record(other)
        with self.assertRaisesRegex(CommandError, "still being restored for"):
            clones.ensure(other)
        self.assertEqual(self.record(other), record)

    def test_unfinished_restore_refuses_when_its_release_record_disappeared(self):
        _, _, _, marker = self.interrupted_restore()
        state = json.loads(marker.read_text())
        self.assertTrue(state["released"])
        state["head"] = None
        marker.write_text(json.dumps(state))
        self.drop_record()
        for _ in range(2):
            with self.assertRaisesRegex(CommandError, "no longer has"):
                clones.ensure(self.item)
        self.assertTrue(marker.is_file())

    def test_unfinished_restore_recognises_its_item_under_another_spelling(self):
        head, bundle, hidden, marker = self.interrupted_restore()
        state = json.loads(marker.read_text())
        state["item"] = str(self.item.dir) + "/"
        marker.write_text(json.dumps(state))
        hidden.rename(bundle)
        self.assertEqual(clones.ensure(self.item)["head"], head)
        self.assertFalse(marker.exists())

    def test_failed_clone_does_not_take_over_another_restore(self):
        other = self.make_item("other")
        self.interrupted_restore()
        marker = self.checkout / ".git" / "factory-restoring"
        before = marker.read_text()
        with self.assertRaises(CommandError):
            clones._clone(self.checkout, self.upstream.as_uri(), {}, restoring={"item": str(other.dir), "branch": "contrib/other", "head": None, "released": True})
        self.assertEqual(marker.read_text(), before)

    def test_new_item_clone_leaves_no_marker(self):
        shutil.rmtree(self.checkout)
        fresh = self.make_item("fresh", create_branch=False)
        real = clones.run

        def local_clone(command, **kwargs):
            if command[:2] == ["git", "clone"]:
                command = [*command[:3], self.upstream.as_uri(), command[4]]
            return real(command, **kwargs)

        with patch.object(clones, "run", side_effect=local_clone):
            result = clones.ensure(fresh)
        self.assertTrue(result["created"])
        self.assertIsNone(result["head"])
        self.assertFalse((self.checkout / ".git" / "factory-restoring").exists())

    def test_submodule_pushed_commit_without_a_local_tracking_ref_can_be_released(self):
        child = self.add_submodule()
        git(child, "checkout", "-b", "feature")
        git(child, "commit", "--allow-empty", "-m", "child feature")
        git(child, "push", self.upstream.as_uri(), "feature")
        git(child, "checkout", "main")
        self.assertTrue(git(child, "rev-list", "feature", "--not", "--remotes"))
        git(self.checkout, "push", "fork", "contrib/fix")
        report = clones.release(self.checkout)
        self.assertTrue(report["released"], report)

    def test_submodule_commit_whose_remote_branch_was_deleted_keeps_checkout(self):
        child = self.add_submodule()
        git(child, "checkout", "-b", "feature")
        git(child, "commit", "--allow-empty", "-m", "child feature")
        git(child, "push", "origin", "feature")
        work = git(child, "rev-parse", "HEAD")
        git(child, "checkout", "main")
        git(self.upstream, "branch", "-D", "feature")
        self.assertFalse(git(self.checkout, "status", "--porcelain"))
        self.assertFalse(git(child, "rev-list", "feature", "--not", "--remotes"))
        report = clones.release(self.checkout)
        self.assertFalse(report["released"], report)
        self.assertIn("submodule modules/child", " ".join(report["why"]))
        self.assertEqual(git(child, "rev-parse", "feature"), work)
        self.assertIsNone(self.record())

    def test_cli_idle_requires_loop_integration(self):
        with patch.object(nextaction, "code_items", None, create=True):
            code, report = self.invoke("release", "--idle")
        self.assertEqual(code, 1)
        self.assertIn("integration", report["error"])
        self.assertTrue(self.checkout.exists())
        with patch.object(nextaction, "code_items", return_value={self.item.dir}, create=True) as active:
            code, report = self.invoke("release", "--idle")
        self.assertEqual(code, 0)
        self.assertEqual(report["reports"], [])
        active.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
