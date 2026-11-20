"""Rebuild a clone that lost objects (L-064).

Refetching over a flaky link keeps failing once objects are missing, so a fresh blobless clone of the default branch
takes the old clone's place, with every contrib/* branch fetched from the old clone and checked to keep its exact sha.
Submit's privacy scan never lazy-fetches, so the blobs it reads for each branch are fetched before the swap.
Work the rebuild does not carry (uncommitted changes, stash entries, commits no remote has on other local branches)
refuses the heal up front and again just before the swap.
The old clone is renamed, not deleted: it stays next to the new one until the owner removes it."""
import os
import pathlib
import re
import shutil

from factory.core import clean
from factory.util import CONFIG, CommandError, git, now, run

COMMAND = "bin/factory clone-heal"
DAMAGED_PACK = ("unresolved deltas", "index-pack failed", "early EOF")
MIN_FREE = 6 * 2**30
MISSING = re.compile(r"^missing \w+ ([0-9a-f]{40,64})$", re.M)
REMOTE_KEYS = ("url", "pushurl", "fetch", "push")


def refresh_hint(clone, error):
    """submit's reason when a base refresh fails the way a clone missing objects fails; None for other failures."""
    mark = next((m for m in DAMAGED_PACK if m in str(error)), None)
    if mark:
        return f"base refresh failed ({mark}); the clone may be missing objects: run {COMMAND} {clone}, then submit again (L-064)"
    return None


def dirty(clone):
    status = run(["git", "-C", str(clone), "status", "--porcelain"], check=False)
    if status.returncode:
        return f"git status failed, so uncommitted changes cannot be ruled out: {status.stderr.strip()[:300]}"
    if status.stdout.strip():
        return "the clone has uncommitted changes; commit or move them before healing"
    return None


def default_branch(clone):
    """origin's default branch as the clone last saw it; empty when unknown, which leaves no branch exempt."""
    return git(clone, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False).removeprefix("refs/remotes/origin/")


def unrestorable(clone):
    """Stash entries, and commits no remote ref reaches on a branch other than the contrib/* branches the rebuild
    carries and the default branch it clones: what a fresh clone cannot bring back."""
    try:
        count = len(clean.stashes(clone))
        default = default_branch(clone)
        branches = [name for name in clean.unpushed_branches(clone) if not name.startswith("contrib/") and name != default]
    except CommandError as error:
        return f"stash entries and unpushed branches cannot be ruled out: {str(error)[:300]}"
    found = [f"{count} stash entr{'y' if count == 1 else 'ies'}"] if count else []
    if branches:
        found.append(f"commits no remote has on {', '.join(branches)}")
    if found:
        return f"a fresh clone cannot restore {' and '.join(found)}; push, apply or drop them before healing"
    return None


def preflight(clone):
    """Why the checkout cannot be swapped without losing work or breaking something that points into it."""
    worktrees = [line for line in git(clone, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")][1:]
    if worktrees:
        return f"{len(worktrees)} linked worktree(s) point into this clone and would break when it moves; remove or prune them first"
    reason = dirty(clone) or unrestorable(clone)
    if reason:
        return reason
    free = shutil.disk_usage(clone.parent).free
    if free < MIN_FREE:
        return f"{free / 2**30:.1f} GiB free next to the clone; a rebuild needs at least {MIN_FREE / 2**30:.0f} GiB"
    if not git(clone, "remote", "get-url", "origin", check=False):
        return "the clone has no origin remote to rebuild from"
    return None


def missing_objects(clone):
    """Missing object ids, or the reason fsck gives none to heal."""
    proc = run(["git", "-C", str(clone), "fsck", "--connectivity-only", "--no-dangling"], check=False)
    found = sorted(set(MISSING.findall(proc.stdout + proc.stderr)))
    if found:
        return found, None
    if proc.returncode:
        return [], f"fsck failed without naming missing objects, which a rebuild does not repair: {proc.stderr.strip()[-300:]}"
    return [], "fsck --connectivity-only reports no missing objects; the refresh failure is not a damaged clone, retry the fetch"


def branch_heads(clone):
    lines = git(clone, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/contrib/").splitlines()
    return {ref.removeprefix("refs/heads/"): sha for ref, sha in (line.split(" ", 1) for line in lines)}


def scan_blobs(repo, head):
    """Both sides of every path the commits on `head` since the default branch change: what submit's privacy scan reads."""
    commits = git(repo, "rev-list", head, "^" + git(repo, "merge-base", "HEAD", head))
    if not commits:
        return set()
    raw = run(["git", "-C", str(repo), "diff-tree", "--stdin", "--root", "-m", "-r", "--raw", "--no-renames"], input=commits + "\n").stdout
    fields = [line.partition("\t")[0].split() for line in raw.splitlines() if line.startswith(":")]
    return {oid for f in fields for mode, oid in zip(f[:2], f[2:4]) if mode.lstrip(":") != "160000" and oid.strip("0")}


def absent(repo, oids):
    if not oids:
        return []
    proc = run(["git", "-C", str(repo), "cat-file", "--batch-check"], input="\n".join(oids) + "\n",
               env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})
    return [line.split()[0] for line in proc.stdout.splitlines() if line.endswith(" missing")]


def fetch_scan_blobs(clone, fresh, heads):
    """Fetch every branch's scan blobs into the rebuild, from the old clone first and origin second; the ids still absent."""
    missing = absent(fresh, sorted(set().union(*(scan_blobs(fresh, sha) for sha in heads.values()))))
    for source in (str(clone), "origin"):
        if missing:
            run(["git", "-C", str(fresh), "-c", "fetch.negotiationAlgorithm=noop", "fetch", "--quiet", "--no-tags",
                 "--no-write-fetch-head", "--recurse-submodules=no", "--stdin", source], input="\n".join(missing) + "\n", check=False)
            missing = absent(fresh, missing)
    return missing


def copy_remotes(old, new):
    entries = run(["git", "-C", str(old), "config", "--local", "--get-regexp", r"^remote\."], check=False).stdout.splitlines()
    values = {}
    for entry in entries:
        key, _, value = entry.partition(" ")
        name, _, field = key[len("remote."):].rpartition(".")
        if field in REMOTE_KEYS:
            values.setdefault((name, field), []).append(value)
    for (name, field), items in values.items():
        git(new, "config", "--unset-all", f"remote.{name}.{field}", check=False)
        for value in items:
            git(new, "config", "--add", f"remote.{name}.{field}", value)


def rebuild(clone, fresh, heads):
    """A blobless clone at `fresh` holding every contrib branch at its old sha; the reason it cannot be, or None."""
    run(["git", "clone", "--quiet", "--filter=blob:none", "--single-branch", git(clone, "remote", "get-url", "origin"), str(fresh)])
    if heads:
        git(fresh, "fetch", "--quiet", str(clone), "refs/heads/contrib/*:refs/heads/contrib/*")
    carried = branch_heads(fresh)
    moved = [f"{name} is {carried.get(name, 'missing')} in the rebuild, {sha} in the old clone"
             for name, sha in heads.items() if carried.get(name) != sha]
    if moved:
        return "branch heads differ: " + "; ".join(moved)
    lost = fetch_scan_blobs(clone, fresh, heads)
    if lost:
        return (f"{len(lost)} blob(s) submit's privacy scan reads for the contrib branches are in neither the old clone "
                f"nor origin ({', '.join(lost[:3])}); old clone untouched")
    copy_remotes(clone, fresh)
    git(fresh, "config", "user.name", CONFIG["git_name"])
    git(fresh, "config", "user.email", CONFIG["git_email"])
    return None


def changed(clone, heads):
    """Why swapping now would strand work done in the old clone since `heads` were read; None when nothing moved.
    Clones are shared per repo, so another item's agent can commit, edit or stash there while the rebuild runs."""
    reason = dirty(clone) or unrestorable(clone)
    if reason:
        return f"rechecked just before the swap, {reason}; rebuild removed"
    if branch_heads(clone) != heads:
        return "contrib branches changed in the old clone during the rebuild; rebuild removed, run clone-heal again"
    return None


def swap(clone, fresh, moved_to):
    os.rename(clone, moved_to)
    try:
        os.rename(fresh, clone)
    except OSError:
        os.rename(moved_to, clone)
        raise


def heal(clone, dry_run=False):
    clone = pathlib.Path(clone).expanduser().resolve()
    report = {"ok": False, "clone": str(clone), "missing_objects": None, "branches": [], "moved_to": None, "refused_reason": None}
    if dry_run:
        report["dry_run"] = True
    if not (clone / ".git").is_dir():
        return {**report, "refused_reason": f"{clone} is not a clone with its own .git directory; worktrees and submodules are not healed"}
    missing, reason = missing_objects(clone)
    report["missing_objects"] = len(missing)
    reason = reason or preflight(clone)
    if reason:
        return {**report, "refused_reason": reason}
    heads = branch_heads(clone)
    report["branches"] = [{"name": name, "sha": sha} for name, sha in heads.items()]
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    moved_to, fresh = (clone.with_name(f"{clone.name}.{tag}-{stamp}") for tag in ("broken", "heal"))
    taken = [str(p) for p in (moved_to, fresh) if os.path.lexists(p)]
    if taken:
        return {**report, "refused_reason": f"already exists: {', '.join(taken)}"}
    if dry_run:
        return {**report, "ok": True, "moved_to": str(moved_to)}
    try:
        reason = rebuild(clone, fresh, heads) or changed(clone, heads)
    except CommandError as error:
        reason = f"rebuild failed, old clone untouched: {error}"
    except BaseException:
        shutil.rmtree(fresh, ignore_errors=True)
        raise
    if reason:
        shutil.rmtree(fresh, ignore_errors=True)
        return {**report, "refused_reason": reason}
    try:
        swap(clone, fresh, moved_to)
    except OSError as error:
        old = clone if clone.exists() else moved_to
        return {**report, "refused_reason": f"swap failed ({error}); old clone at {old}, rebuild at {fresh}"}
    return {**report, "ok": True, "moved_to": str(moved_to)}
