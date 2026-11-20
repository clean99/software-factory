"""Red→green proof: the tests in the change must fail without the fix and pass with it.

Runs in the item's clone so installed dependencies are reused. Non-test files changed by the
branch are temporarily reverted to the merge base (added files removed), the test command runs
and must fail; then everything is restored and the command must pass. A test that passes on
the base proves nothing about the fix, which is the most common hollow test in AI-written PRs.
"""
import base64
import os
import pathlib
import stat
import subprocess
import tempfile

from factory.core.slop import TEST_PATH, DOC_PATH
from factory.util import git, iso, write_json


def _run(cmd, cwd, log_path, timeout):
    with tempfile.TemporaryDirectory(prefix="factory-test-cache-") as cache, open(log_path, "w") as log:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": cache}
        try:
            proc = subprocess.run(cmd, cwd=cwd, shell=True, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, env=env)
            return proc.returncode
        except subprocess.TimeoutExpired:
            log.write(f"\n[factory] timed out after {timeout}s\n")
            return 124


def path_state(path):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return {"kind": "absent"}
    content = b""
    if stat.S_ISLNK(mode):
        kind, content = "symlink", os.fsencode(os.readlink(path))
    elif stat.S_ISREG(mode):
        kind, content = "file", path.read_bytes()
    else:
        kind = "directory" if stat.S_ISDIR(mode) else "special"
    return {"kind": kind, "mode": mode, "content": base64.b64encode(content).decode("ascii")}


def restore_path(clone, ref, path):
    target = clone / path
    for parent in target.parents:
        if parent == clone:
            break
        if parent.is_symlink():
            raise RuntimeError(f"refusing restoration through symlinked parent: {path}")
    if git(clone, "ls-tree", ref, "--", path):
        git(clone, "restore", f"--source={ref}", "--worktree", "--", path)
    elif target.is_file() or target.is_symlink():
        target.unlink()
    elif target.exists():
        raise RuntimeError(f"expected file, found directory while restoring {path}")


def publish_result(path, result):
    with tempfile.NamedTemporaryFile(prefix=".redgreen-", suffix=".json", dir=path.parent, delete=False) as stream:
        pending = pathlib.Path(stream.name)
    try:
        write_json(pending, result)
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def run(item, timeout=1800):
    item.file("redgreen.json").unlink(missing_ok=True)
    recovery = item.file("restoration.json")
    if recovery.exists():
        raise SystemExit("an earlier restoration.json remains; inspect it before retrying red→green")
    meta, clone = item.meta, item.clone
    cmd = meta.get("test_cmd")
    if not cmd:
        raise SystemExit("item.json needs test_cmd (the focused test command, run from test_cwd)")
    cwd = clone / meta.get("test_cwd", ".")
    branch = meta["branch"]

    if git(clone, "rev-parse", "--abbrev-ref", "HEAD") != branch:
        git(clone, "checkout", branch)
    if git(clone, "status", "--porcelain", "--untracked-files=no"):
        raise SystemExit(f"{clone} has uncommitted changes; commit or stash before red→green")

    base = item.merge_base()
    head = item.head_sha()
    explicit_tests = set(meta.get("test_files", []))
    changes = []
    for line in git(clone, "diff", "--name-status", "--no-renames", f"{base}..{head}").splitlines():
        status, path = line.split("\t", 1)
        is_test = path in explicit_tests or bool(TEST_PATH.search(path))
        if not is_test and not DOC_PATH.search(path):
            changes.append((status, path))
    if not changes:
        raise SystemExit("no non-test source changes to revert; red→green needs a fix to remove")
    for status, path in changes:
        if status == "D" and os.path.lexists(clone / path):
            raise SystemExit(f"restoration would overwrite an existing local path: {path}; move it aside before retrying")

    original = {path: path_state(clone / path) for _, path in changes}
    red_log, green_log = item.file("redgreen-red.log"), item.file("redgreen-green.log")
    write_json(recovery, {"head_sha": head, "reverted": list(original)})
    mutated = False
    try:
        for _, path in changes:
            restore_path(clone, base, path)
        expected = git(clone, "diff", "--binary", "--no-ext-diff", head)
        before = {path: path_state(clone / path) for _, path in changes}
        try:
            red = _run(cmd, cwd, red_log, timeout)
        finally:
            after = {path: path_state(clone / path) for _, path in changes}
            actual = git(clone, "diff", "--binary", "--no-ext-diff", head)
            mutations = [{"path": path, "before": before[path], "after": after[path]} for path in before if before[path] != after[path]]
            mutated = bool(mutations) or expected != actual
            if mutated:
                item.file("redgreen-mutations.patch").write_text(actual)
                write_json(item.file("redgreen-mutations.json"), {"paths": mutations})
    finally:
        problems = []
        for _, path in changes:
            try:
                restore_path(clone, head, path)
                if original[path]["kind"] == "file":
                    (clone / path).chmod(stat.S_IMODE(original[path]["mode"]))
                if path_state(clone / path) != original[path]:
                    problems.append(f"{path}: restoration verification failed")
            except (OSError, RuntimeError) as error:
                problems.append(f"{path}: {error}")
        if problems or git(clone, "status", "--porcelain", "--untracked-files=no"):
            raise SystemExit(f"{clone} not restored cleanly after red phase; inspect restoration.json: {'; '.join(problems)}")
        recovery.unlink()
    green = None if mutated else _run(cmd, cwd, green_log, timeout)

    result = {
        "at": iso(),
        "state": "complete",
        "base_sha": base,
        "head_sha": head,
        "cmd": cmd,
        "cwd": str(pathlib.Path(meta.get("test_cwd", "."))),
        "reverted": [p for _, p in changes],
        "red_exit": red,
        "green_exit": green,
        "ok": not mutated and 0 < red < 124 and green == 0,
    }
    publish_result(item.file("redgreen.json"), result)
    return result
