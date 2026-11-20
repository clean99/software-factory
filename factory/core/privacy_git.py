from bisect import bisect_right
import os
import pathlib
import re
import subprocess

from factory.core import images, leaks, privacy, published


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def git(repo, *args, check=True, input=None, lazy=False):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_NO_LAZY_FETCH="0" if lazy else "1", GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
    try:
        result = subprocess.run(["git", "--no-replace-objects", "-C", str(repo), *args],
                                env=env, input=input, capture_output=True, timeout=120 if lazy else 5)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("git read failed or timed out; privacy scan incomplete") from None
    if check and result.returncode:
        raise ValueError("git read failed; privacy scan incomplete")
    return result.stdout if result.returncode == 0 else b""


def fetch_scan_blobs(repo, revisions):
    """Hydrate only blobs the outgoing diffs read, including historical and merge-parent copies."""
    raw = git(repo, "log", "--format=", "--raw", "-z", "--no-abbrev", "--no-renames", "-m", "--root", *revisions, "--")
    objects = set()
    for field in raw.split(b"\0"):
        match = re.fullmatch(rb":(\d{6}) (\d{6}) ([0-9a-f]{40,64}) ([0-9a-f]{40,64}) [A-Z]\d*", field.strip())
        if match:
            for mode, oid in ((match[1], match[3]), (match[2], match[4])):
                if mode != b"160000" and oid.strip(b"0"):
                    objects.add(oid)
    if not objects:
        return
    rows = git(repo, "cat-file", "--batch-check", input=b"\n".join(sorted(objects)) + b"\n")
    for row in rows.splitlines():
        oid, *info = row.split()
        if info == [b"missing"]:
            # cat-file asks the configured promisor remote for this exact object. No ref is moved.
            git(repo, "cat-file", "-e", oid.decode("ascii"), lazy=True)
            git(repo, "cat-file", "-e", oid.decode("ascii"))


def scan(repo, revisions, *, names=(), allow=(), binary_reviewed=False, item_dir=None, on_pr_branch=(),
         reviewed_source_lines=None):
    found = []
    if git(repo, "rev-parse", "--is-shallow-repository").strip() == b"true":
        found.append("shallow history; outgoing privacy scan incomplete")
    for name in names:
        found += privacy.findings("outgoing ref", name, allow)
    commits = git(repo, "rev-list", *revisions).decode().splitlines()
    if len(commits) > 1000:
        return ["outgoing history exceeds 1000 commits; split or use a reviewed factory item"]
    fetch_scan_blobs(repo, revisions)
    metadata = list(names)
    total = 0
    for sha in commits:
        raw = git(repo, "cat-file", "commit", sha).decode("utf-8", errors="replace")
        metadata.append(raw)
        waive = published.identity_waiver(published.raw_identity_lines(raw)) if sha in on_pr_branch else ()
        found += privacy.findings(sha[:9] + " commit", raw, allow, prose=True, waive=waive)
        for filename, oldmodes, mode, olds, oid, status in introduced(repo, sha):
            label = sha[:9] + ":" + filename
            found += privacy.findings(label + " [path]", filename, allow)
            if status == "D":
                continue
            if mode == "160000":
                found.append(privacy.redact(label) + ": submodule requires separate privacy scan")
                continue
            size = int(git(repo, "cat-file", "-s", oid))
            total += size
            if size > 20_000_000 or total > 100_000_000:
                return found + ["outgoing content too large; privacy scan incomplete"]
            data = git(repo, "cat-file", "blob", oid)
            suffix = pathlib.Path(filename).suffix.lower()
            if suffix in images.EXTENSIONS:
                found += images.findings(label, data, suffix)
                if not binary_reviewed:
                    found.append(privacy.redact(label) + ": binary requires explicit visual review")
                continue
            try:
                text = data.decode("utf-8")
                binary = "\x00" in text
            except UnicodeDecodeError:
                binary, text = True, data.decode("utf-8", errors="replace")
            if binary and not binary_reviewed:
                found.append(privacy.redact(label) + ": binary requires explicit review")
            # One list of changed line ranges per parent; None means the file is new against that parent.
            spans = [changed_ranges(repo, old, oid) if old.strip("0") and oldmode != ":160000" and not binary else None
                     for oldmode, old in zip(oldmodes, olds)]
            newlines = [m.start() for m in re.finditer("\n", text)]
            prose = suffix in {".md", ".mdx", ".txt", ".rst"} or pathlib.Path(filename).name.upper().startswith("README")
            safe_label = privacy.redact(label)
            lines = text.split("\n")
            reviewed = (reviewed_source_lines or {}).get(filename, [])
            for start, end, why, value in privacy.matches(text, prose=prose):
                first = bisect_right(newlines, start - 1) + 1
                last = bisect_right(newlines, max(start, end - 1) - 1) + 1
                if value in allow or text[start:end] in allow:
                    continue
                if first == last and privacy.reviewed_source_line(lines[first - 1], why, reviewed):
                    continue
                if all(touches(ranges, first, last) for ranges in spans):
                    found.append(f"{safe_label}:{first}: {privacy.masked(value, why)}")
    found += leaks.scan(text="\n".join(metadata), item_dir=item_dir, label="outgoing metadata")
    found += leaks.scan(repo=repo, revisions=" ".join(revisions) + " -m --no-ext-diff --no-textconv --", item_dir=item_dir,
                        label="outgoing commits", carried=carried_by_merge(repo))
    return list(dict.fromkeys(found))


def introduced(repo, sha):
    """(path, old modes, mode, old blobs, blob, status) for each path the commit writes, with one old
    mode and blob per parent. A merge commit keeps only paths that differ from every parent: a file taken
    whole from one parent was scanned with that parent's own commits or is already public. Comparing a
    merge with each parent separately (diff-tree -m) re-reported the upstream files it carried in (oxc
    #28150's merge of main brought 1,797 upstream-identical files and 33 upstream findings, 2026-11-05).
    So every revision scan() excludes must be public: a local lower branch left out of the range would let
    a merge carry its unscanned content (slop's stacked items, 2026-11-06). The rule is per line, so lines
    each kept from a different parent are not re-checked as a whole."""
    parents = git(repo, "rev-parse", sha + "^@").decode().split()
    per_parent = []
    for parent in parents or [None]:
        args = [parent, sha] if parent else ["--root", "--no-commit-id", sha]
        raw = git(repo, "diff-tree", "-r", "--raw", "-z", "--no-renames", *args).split(b"\0")
        entries = {}
        for i in range(0, len(raw) - 1, 2):
            entries[os.fsdecode(raw[i + 1])] = raw[i].decode("ascii").split()
        per_parent.append(entries)
    for filename, (oldmode, mode, old, oid, status) in per_parent[0].items():
        others = [entries.get(filename) for entries in per_parent[1:]]
        if None in others:
            continue
        yield filename, [oldmode] + [o[0] for o in others], mode, [old] + [o[2] for o in others], oid, status


def changed_ranges(repo, old, oid):
    patch = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--text", "--unified=0", old, oid).decode("utf-8", errors="replace")
    return [(int(m[1]), int(m[1]) + int(m[2] or 1) - 1) for m in HUNK.finditer(patch) if int(m[2] or 1)]


def touches(ranges, first, last):
    """Whether lines first..last overlap a changed range; None (a new file) always does."""
    if ranges is None:
        return True
    index = bisect_right([lo for lo, _ in ranges], last) - 1
    return index >= 0 and ranges[index][1] >= first


def carried_by_merge(repo):
    """gitleaks reads a merge once per parent (-m). A finding there counts only where the merge's
    content differs from every parent, the same rule introduced() applies to the privacy scan.
    gitleaks numbers a match on lines a parent already has from that parent's file, not the merge's;
    a secret the merge writes is an added line against every parent and keeps the merge's numbering."""
    cache = {}

    def check(row):
        sha = row.get("Commit") or ""
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            return False
        if sha not in cache:
            parents = git(repo, "rev-parse", sha + "^@").decode().split()
            written = {path: (modes, olds, oid) for path, modes, _, olds, oid, _ in introduced(repo, sha)} if len(parents) > 1 else None
            cache[sha] = (parents, written)
        parents, written = cache[sha]
        if written is None:
            return False
        path = row.get("File") or ""
        if path not in written:
            # Dropped only when the file provably equals a parent's copy; a path we cannot match
            # (quoting, encoding) keeps the finding.
            blob = git(repo, "rev-parse", "--verify", "-q", f"{sha}:{path}", check=False).strip()
            return bool(blob) and any(git(repo, "rev-parse", "--verify", "-q", f"{parent}:{path}", check=False).strip() == blob
                                      for parent in parents)
        modes, olds, oid = written[path]
        first = int(row["StartLine"])
        last = max(first, int(row.get("EndLine") or first))
        spans = [changed_ranges(repo, old, oid) if old.strip("0") and mode != ":160000" else None for mode, old in zip(modes, olds)]
        return not all(touches(ranges, first, last) for ranges in spans)

    return check
