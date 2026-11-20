"""Disk hygiene for clones and worktrees; use bin/factory clone release --idle for idle clones.

Dependency and build output goes once no item in a checkout is being built or waiting to be submitted (submit re-runs
the tests): an install recreates it. A checkout itself goes only when no live item uses it and a fresh clone would
restore everything in it: no unpushed commit, no stash, no uncommitted change, no other worktree hanging off it."""
import os
import pathlib
import shutil

from factory.core import ledger
from factory.util import ROOT, Item, git, path_from_config, run

REGENERABLE = {"node_modules", "target", ".turbo", ".next", ".nx", ".pnpm-store", ".venv", ".venv-typecheck"}
DONE = ledger.DONE
BUSY = {"preparing", "ready", "approved"}


def roots():
    return [path_from_config("repos_dir"), ROOT / "workspace"]


def is_checkout(path):
    return (path / ".git").exists()


def checkouts():
    """Checkouts one or two levels below each root (workspace groups worktrees by run); never descends into one."""
    found = []
    for root in roots():
        if not root.is_dir():
            continue
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            if is_checkout(d):
                found.append(d.resolve())
            else:
                found += [s.resolve() for s in sorted(d.iterdir()) if s.is_dir() and is_checkout(s)]
    return found


def item_statuses():
    """checkout -> statuses of the items built in it; the ledger's view of a PR wins over item.json."""
    by_item = {pathlib.Path(r["item"]).resolve(): r["status"] for r in ledger.load() if r.get("item")}
    uses = {}
    runs = path_from_config("runs_dir")
    for meta_path in [*runs.glob("*/*/item.json"), *runs.glob("*/*/*/item.json")]:
        item = Item(meta_path.parent)
        status = by_item.get(item.dir, item.meta.get("status"))
        uses.setdefault(item.clone.resolve(), set()).add(status)
    return uses


def regenerable_dirs(checkout):
    """Ignored dependency/build directories that hold no tracked file (some repos commit node_modules fixtures)."""
    hits = []
    for dirpath, dirnames, filenames in os.walk(checkout):
        here = pathlib.Path(dirpath)
        if here != checkout and (".git" in dirnames or ".git" in filenames):
            dirnames[:] = []
            continue
        hits += [here / n for n in dirnames if n in REGENERABLE]
        dirnames[:] = [n for n in dirnames if n not in REGENERABLE and n != ".git"]
    if not hits:
        return []
    listed = run(["git", "-C", str(checkout), "check-ignore", "--stdin"], check=False, input="\n".join(map(str, hits)) + "\n")
    ignored = [pathlib.Path(line) for line in listed.stdout.splitlines() if line]
    if not ignored:
        return []
    tracked = git(checkout, "ls-files", "--", *map(str, ignored)).splitlines()
    holding = {h for h in ignored for t in tracked if (checkout / t).is_relative_to(h)}
    return [h for h in ignored if h not in holding]


def size(path):
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_blocks * 512
            except OSError:
                pass
    return total


def stashes(checkout, check=True):
    return git(checkout, "stash", "list", check=check).splitlines()


def unpushed_branches(checkout, check=True):
    """Local branches whose tip no remote ref reaches; a tip a remote reaches brings its whole history with it."""
    tips = {}
    for line in git(checkout, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/", check=check).splitlines():
        sha, ref = line.split(" ", 1)
        tips.setdefault(sha, []).append(ref.removeprefix("refs/heads/"))
    unpushed = set(git(checkout, "rev-list", *tips, "--not", "--remotes", check=check).split()) if tips else set()
    return sorted(name for sha in tips.keys() & unpushed for name in tips[sha])


def unique_work(checkout, worktree):
    """What deleting this checkout would lose; empty means a fresh clone brings all of it back."""
    lost = []
    unpushed = git(checkout, "log", "HEAD", "--not", "--remotes", "--oneline", check=False) if worktree else unpushed_branches(checkout, check=False)
    if unpushed:
        lost.append("unpushed commits")
    if not worktree and stashes(checkout, check=False):
        lost.append("stash")
    if git(checkout, "status", "--porcelain", check=False):
        lost.append("uncommitted changes")
    if not worktree:
        git(checkout, "worktree", "prune", check=False)
        others = [line for line in git(checkout, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")][1:]
        if others:
            lost.append(f"{len(others)} worktree(s)")
    return lost


def shown(path):
    home = pathlib.Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)


def drop_deps(targets, dry_run=False):
    removed, freed = [], 0
    for checkout in targets:
        for d in regenerable_dirs(pathlib.Path(checkout).resolve()):
            freed += size(d)
            removed.append(shown(d))
            if not dry_run:
                shutil.rmtree(d, ignore_errors=True)
    return removed, freed


def after_submit(item):
    """The PR branch lives on the fork now; free the checkout unless another item is still building in it."""
    checkout = item.clone.resolve()
    if item_statuses().get(checkout, set()) & BUSY:
        return "kept: another item in this checkout is still being built or submitted"
    removed, freed = drop_deps([checkout])
    return f"{len(removed)} dirs, {freed / 2**30:.1f} GB"


def run_clean(prune=False, dry_run=False):
    before = shutil.disk_usage(ROOT).free
    uses = item_statuses()
    found = checkouts()
    busy = [c for c in found if uses.get(c, set()) & BUSY]
    removed, freed = drop_deps([c for c in found if c not in busy], dry_run)
    report = {"dry_run": dry_run, "deps_removed": len(removed), "deps_bytes": freed, "deps_kept": [shown(c) for c in busy]}
    if prune:
        pruned, kept = [], []
        worktrees = {c for c in found if (c / ".git").is_file()}
        for checkout in sorted(found, key=lambda c: c not in worktrees):
            live = sorted(s or "?" for s in uses.get(checkout, set()) - DONE)
            worktree = checkout in worktrees
            why = [f"items {', '.join(live)}"] if live else unique_work(checkout, worktree)
            if why:
                kept.append({"path": shown(checkout), "why": why})
                continue
            pruned.append(shown(checkout))
            if dry_run:
                continue
            if worktree:
                common = pathlib.Path(git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"))
                git(common, "worktree", "remove", str(checkout))
            else:
                shutil.rmtree(checkout)
        report.update(pruned=pruned, kept=kept)
    after = shutil.disk_usage(ROOT).free
    report.update(freed_gb=round((after - before) / 2**30, 2), free_gb=round(after / 2**30, 2))
    return report
