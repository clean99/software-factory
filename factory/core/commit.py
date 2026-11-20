"""Commit and push the factory's own checkout: the coordinator's stage, scan, commit and push chain as one command."""
import json
import os
import pathlib

from factory.core import publication
from factory.core.leaks import CONFIG_FILE
from factory.util import CONFIG, ROOT, CommandError, git, run

# Same limit as .githooks/pre-commit.
LIMIT = 2 * 1024 * 1024
# The scan runs in leaks.scan's scrubbed environment; these would make git stage and commit another index than the one scanned.
PINNED = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")
# gitleaks logs these and still exits 0 when git diff failed or the scan stopped early.
SCAN_ERRORS = (" ERR ", "stderr is not empty", "partial scan")
LOG_POLICY = ("Keep raw validation logs local in the shift's run directory; do not commit them or rename them to .txt to bypass ignore rules. "
              "Commit a concise result with the command, exit code and local log path instead.")


def preflight(paths):
    pinned = [k for k in PINNED if k in os.environ]
    if pinned:
        raise ValueError("unset " + ", ".join(pinned) + ": the secret scan only reads the factory's own index")
    root = ROOT.resolve()
    outside = [p for p in paths if not pathlib.Path(p).resolve().is_relative_to(root)]
    if outside:
        raise ValueError("paths outside the factory: " + ", ".join(outside))
    branch = git(ROOT, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    if branch != "main":
        raise ValueError(f"factory records must be committed on main; HEAD is {branch or 'detached'}. "
                         "Return to main without discarding work; do not create a temporary branch for records.")
    return branch, ROOT / git(ROOT, "rev-parse", "--git-path", "index")


def state():
    """HEAD and the index entries: what another git process changes when it stages or commits."""
    return git(ROOT, "rev-parse", "-q", "--verify", "HEAD", check=False), git(ROOT, "ls-files", "--stage", "-z")


class Index:
    """The index before staging, put back on refusal only if no other git process changed HEAD or the entries since this command staged."""

    def __init__(self, path):
        self.path, self.after = path, None
        try:
            with open(path, "rb") as handle:
                self.before, self.stat = handle.read(), os.fstat(handle.fileno())
        except FileNotFoundError:
            self.before = self.stat = None

    def mark(self):
        self.after = state()

    def restore(self):
        if self.after is None:
            return None
        lock = self.path.with_name(self.path.name + ".lock")
        try:
            handle = open(lock, "xb")
        except OSError as error:
            return f"could not take {lock} ({error.strerror})"
        try:
            with handle:
                if state() != self.after:
                    raise ValueError("another git process changed HEAD or the index after this command staged")
                if self.before is None:
                    self.path.unlink(missing_ok=True)
                    lock.unlink()
                    return None
                handle.write(self.before)
            # Git re-reads entries written in the same second as the index (racy git); a new mtime would hide those edits.
            os.utime(lock, ns=(self.stat.st_atime_ns, self.stat.st_mtime_ns))
            os.replace(lock, self.path)
        except (OSError, ValueError, CommandError) as error:
            lock.unlink(missing_ok=True)
            return str(error)
        return None


def stage(paths, index):
    if paths:
        selected = [pathlib.Path(os.path.abspath(p)) for p in paths]
        staged = git(ROOT, "diff", "--cached", "--name-only", "--no-renames", "-z").split("\0")
        outside = [name for name in staged if name and not any((ROOT / name).is_relative_to(p) for p in selected)]
        if outside:
            raise ValueError("staged changes outside --path; preserve or commit them separately: " + ", ".join(outside))
        ignored = run(["git", "-C", str(ROOT), "check-ignore", "-z", "--stdin"],
                      input="\0".join(map(str, selected)) + "\0", check=False)
        if ignored.returncode not in (0, 1):
            raise CommandError(ignored.stderr.strip() or "could not check ignored paths")
        if ignored.stdout:
            names = [name for name in ignored.stdout.split("\0") if name]
            reason = "ignored paths are not commit records: " + ", ".join(names)
            if any(pathlib.Path(name).suffix == ".log" for name in names):
                reason += ". " + LOG_POLICY
            raise ValueError(reason)
        deleted = {ROOT / name for name in git(ROOT, "diff", "--cached", "--name-only", "--no-renames",
                                              "--diff-filter=D", "-z").split("\0") if name}
        # A staged deletion is already absent from the index; git add cannot match it unless it was recreated.
        to_add = [p for p in selected if p not in deleted or os.path.lexists(p)]
        args = ["--literal-pathspecs", "add", "--", *map(str, to_add)] if to_add else []
    else:
        args = ["add", "-u"]
    try:
        if args:
            git(ROOT, *args)
    finally:
        # A refused add can still stage valid paths before reporting an ignored path.
        index.mark()
    tree = git(ROOT, "write-tree")
    return tree, changes(tree)


def changes(tree):
    """What the tree changes against HEAD: the files this command scans and commits."""
    base = git(ROOT, "rev-parse", "-q", "--verify", "HEAD^{tree}", check=False) or git(ROOT, "hash-object", "-t", "tree", os.devnull)
    fields = [f for f in git(ROOT, "diff-tree", "-r", "--name-status", "-z", base, tree).split("\0") if f]
    return list(zip(fields[0::2], fields[1::2]))


def oversized(tree, paths):
    if not paths:
        return []
    sizes = run(["git", "-C", str(ROOT), "cat-file", "--batch-check=%(objectsize)"],
                input="".join(f"{tree}:{p}\n" for p in paths)).stdout.splitlines()
    if len(sizes) != len(paths) or not all(s.isdigit() for s in sizes):
        raise CommandError("could not read the size of every staged file")
    return [f"{p} ({s} bytes)" for p, s in zip(paths, sizes) if int(s) > LIMIT]


def scan_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLEAKS_", "GIT_"))}
    env.update(GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return env


def leaks():
    # gitleaks always loads <source>/.gitleaksignore; no flag turns that off.
    if os.path.lexists(ROOT / ".gitleaksignore"):
        return ".gitleaksignore in the factory root would waive gitleaks findings; remove it (factory commit takes no waivers)"
    command = ["gitleaks", "protect", "--staged", "--redact", "--no-banner", "--no-color", "--config", str(CONFIG_FILE),
               "--gitleaks-ignore-path", os.devnull, "--ignore-gitleaks-allow", "--report-format", "json", "--report-path", "-"]
    try:
        proc = run(command, cwd=ROOT, check=False, env=scan_env(), timeout=300)
    except (OSError, CommandError) as error:
        return f"gitleaks could not run ({error})"
    if proc.returncode == 0:
        errors = [line.strip() for line in proc.stderr.splitlines() if any(e in line for e in SCAN_ERRORS)]
        if not errors:
            return None
        return "gitleaks exited 0 but the scan did not finish: " + "; ".join(errors)[-500:]
    try:
        found = [f"{r['RuleID']} in {r['File']}:{r['StartLine']}" for r in json.loads(proc.stdout)]
    except (ValueError, KeyError, TypeError):
        found = []
    if not found:
        return f"gitleaks exited {proc.returncode} without a readable report: {proc.stderr.strip()[-500:]}"
    return f"gitleaks found {len(found)} leak(s): " + "; ".join(found)


def check(tree, changes):
    big = oversized(tree, [p for status, p in changes if status != "D"])
    if big:
        raise ValueError(f"staged files over {LIMIT} bytes: " + ", ".join(big))
    found = leaks()
    if found:
        raise ValueError(found)


def unchanged(tree):
    now = git(ROOT, "write-tree")
    if now != tree:
        raise ValueError(f"the index changed after the secret scan (scanned tree {tree}, index now {now}); nothing was committed")


def commit(message):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    run(["git", "-C", str(ROOT), "-c", f"user.name={CONFIG['git_name']}", "-c", f"user.email={CONFIG['git_email']}",
         "commit", "-q", "-m", message], env=env)


def run_commit(message, paths=(), push=True):
    result = {"ok": False, "commit": None, "pushed": False, "staged": [], "refused_reason": None}
    try:
        branch, path = preflight(paths)
        index = Index(path)
    except (OSError, ValueError, CommandError) as error:
        return {**result, "refused_reason": str(error)}
    try:
        tree, changes = stage(paths, index)
        result["staged"] = [f"{status} {p}" for status, p in changes]
        if changes:
            check(tree, changes)
            unchanged(tree)
            commit(message)
    except (OSError, ValueError, CommandError) as error:
        kept = index.restore()
        reason = f"{error}; the index still holds what this command staged: {kept}" if kept else str(error)
        return {**result, "refused_reason": reason}
    if not changes:
        return {**result, "ok": True}
    result["commit"] = git(ROOT, "rev-parse", "HEAD")
    committed = git(ROOT, "rev-parse", "HEAD^{tree}")
    if committed != tree:
        return {**result, "refused_reason": f"commit {result['commit']} has tree {committed}, not the scanned tree {tree}; it stays local and was not pushed"}
    if push:
        code = publication.run("push", ["origin", branch], cwd=ROOT)
        if code:
            return {**result, "refused_reason": f"committed locally; push to origin {branch} failed (exit {code})"}
        result["pushed"] = True
    return {**result, "ok": True}
