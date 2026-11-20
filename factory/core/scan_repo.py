import hashlib
import json
import os
import pathlib
import re
import subprocess
import tarfile
import zipfile
import zlib

from factory.core import images, leaks, privacy, slop
from factory.core.privacy import placeholder_host
from factory.util import CONFIG, CommandError


ADDED_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
def matches(text, path=""):
    return privacy.matches(text, path, markers=slop.identity_markers())


def load_allow(path):
    if path is None:
        return set()
    try:
        rows = json.loads(pathlib.Path(path).expanduser().read_text())
    except (OSError, ValueError):
        raise CommandError("scan-repo: cannot read allow file as JSON") from None
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or set(row) != {"string", "reason"}
        or not isinstance(row["string"], str) or not row["string"]
        or not isinstance(row["reason"], str) or not row["reason"].strip()
        for row in rows
    ):
        raise CommandError('scan-repo: allow file must be [{"string": "exact match", "reason": "why it is fake"}]')
    return {row["string"] for row in rows}


def safe_label(text):
    spans = [(start, end) for start, end, _, _ in matches(text)]
    for value in privacy.identity_markers() + list(CONFIG.get("private_terms", [])) + list(CONFIG.get("private_repos", [])):
        if value:
            spans.extend((m.start(), m.end()) for m in re.finditer(re.escape(value), text, re.I))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        text = text[:start] + "[redacted]" + text[end:]
    return text


def decode(data):
    try:
        text = data.decode("utf-8")
        return text, "\x00" in text
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), True


def run(path, allow_file=None, artifacts=None):
    repo = pathlib.Path(path).expanduser().resolve()
    allow = load_allow(allow_file)
    findings, seen = [], set()
    leak_cache = {}
    fingerprints = {value.removeprefix("gitleaks:") for value in allow if value.startswith("gitleaks:")}
    stats = {"commits": 0, "head_files": 0, "untracked_files": 0, "refs": 0}

    def git(*args, check=True):
        try:
            proc = subprocess.run(
                ["git", "--no-replace-objects", "-C", str(repo), *args],
                capture_output=True, env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
            )
        except OSError:
            raise CommandError("scan-repo: cannot run git") from None
        if check and proc.returncode:
            raise CommandError("scan-repo: git read failed; scan incomplete")
        return proc.stdout if proc.returncode == 0 else b""

    def add(location, detector):
        location = safe_label(location)
        if (location, detector) not in seen:
            seen.add((location, detector))
            findings.append({"location": location, "detector": detector})

    def scan(label, text, filename="", ranges=None):
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest not in leak_cache:
            leak_cache[digest] = leaks.scan(text=text, label="content", fingerprints=fingerprints)
        for finding in leak_cache[digest]:
            line = re.search(r" at line (\d+);", finding)
            add(f"{label}:{line[1] if line else 1}", finding.removeprefix("content: "))
        for start, end, detector, value in matches(text, filename):
            if value in allow or text[start:end] in allow:
                continue
            first = text.count("\n", 0, start) + 1
            last = text.count("\n", 0, max(start, end - 1)) + 1
            if ranges is None or any(first <= hi and last >= lo for lo, hi in ranges):
                add(f"{label}:{first}", detector)

    def reviewed(data):
        return f"binary:{hashlib.sha256(data).hexdigest()}" in allow

    def scan_image(label, filename, data):
        suffix = pathlib.Path(filename).suffix.lower()
        if suffix in images.EXTENSIONS:
            for finding in images.findings(label, data, suffix):
                add(label + ":1", finding.split(": ", 1)[-1])
            if not reviewed(data):
                add(label + ":1", "binary file requires review")
            return True
        return False

    def scan_content(label, filename, data):
        if scan_image(label, filename, data):
            return
        text, binary = decode(data)
        if binary and not reviewed(data):
            add(label + ":1", "binary file requires review")
        scan(label, text, filename)

    def scan_artifact(path):
        path = pathlib.Path(path).expanduser()
        label = "artifact " + path.name
        scan(label + " [path]", path.name)
        try:
            if path.name.lower().endswith((".tar.gz", ".tgz", ".tar")):
                with tarfile.open(path, "r:*") as archive:
                    for member in archive:
                        member_label = label + "!/" + member.name
                        scan(member_label + " [path]", member.name)
                        if member.uid != 0 or member.gid != 0 or member.uname not in ("", "root", "wheel") or member.gname not in ("", "root", "wheel"):
                            add(member_label + ":1", "archive owner")
                        if member.isfile():
                            with archive.extractfile(member) as content:
                                scan_content(member_label, member.name, content.read())
                        elif member.issym() or member.islnk():
                            scan(member_label, member.linkname)
                        elif not member.isdir():
                            add(member_label + ":1", "archive entry requires review")
            elif path.name.lower().endswith((".zip", ".whl")):
                with zipfile.ZipFile(path) as archive:
                    for member in archive.infolist():
                        member_label = label + "!/" + member.filename
                        scan(member_label + " [path]", member.filename)
                        if not member.is_dir():
                            scan_content(member_label, member.filename, archive.read(member))
            else:
                raise CommandError("scan-repo: unsupported artifact format; scan incomplete")
        except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile, RuntimeError, EOFError, zlib.error):
            raise CommandError("scan-repo: cannot read artifact; scan incomplete") from None

    def scan_blob(label, filename, mode, oid, old=None):
        scan(label + " [path]", filename)
        if mode == "160000":
            add(label + ":1", "submodule requires separate scan")
            return
        data = git("cat-file", "blob", oid)
        if scan_image(label, filename, data):
            return
        text, binary = decode(data)
        if binary and not reviewed(data):
            add(label + ":1", "binary file requires review")
        ranges = None
        if old and old.strip("0") and not binary:
            ranges = []
            diff = git("diff", "--no-ext-diff", "--no-textconv", "--no-color", "--text", "--unified=0", old, oid).decode("utf-8", errors="replace")
            for line in diff.splitlines():
                m = ADDED_HUNK.match(line)
                if m:
                    start, count = int(m[1]), int(m[2] or 1)
                    if count:
                        ranges.append((start, start + count - 1))
        scan(label, text, filename, ranges)

    scan("repository", "")
    bare = git("rev-parse", "--is-bare-repository").strip() == b"true"
    if not bare:
        repo = pathlib.Path(os.fsdecode(git("rev-parse", "--show-toplevel").rstrip(b"\n")))
    if git("rev-parse", "--is-shallow-repository").strip() == b"true":
        add("repository:1", "shallow history; scan incomplete")
    head = git("rev-parse", "--verify", "HEAD", check=False).strip().decode("ascii")
    if head:
        for entry in git("ls-tree", "-rz", head).split(b"\0"):
            if not entry:
                continue
            header, filename = entry.split(b"\t", 1)
            mode, _, oid = header.decode("ascii").split()
            filename = os.fsdecode(filename)
            stats["head_files"] += 1
            scan_blob(filename, filename, mode, oid)
    refs = git("for-each-ref", "--format=%(refname)%00%(objecttype)%00%(objectname)").splitlines()
    stats["refs"] = len(refs)
    for row in refs:
        name, kind, oid = row.split(b"\0")
        name = os.fsdecode(name)
        scan("ref " + name, name)
        if kind == b"tag":
            tag = git("cat-file", "tag", oid.decode("ascii")).decode("utf-8", errors="replace")
            headers, _, message = tag.partition("\n\n")
            scan("tag " + name + " [object]", tag)
            scan("tag " + name, message)
            for line in headers.splitlines():
                if line.startswith("tagger "):
                    identity = re.match(r"tagger (.*) <([^<>]*)>", line)
                    if not identity or identity.groups() != (CONFIG["git_name"], CONFIG["git_email"]):
                        add("tag " + name + ":1", "tagger identity")
                    if identity:
                        scan("tag " + name + " [tagger]", identity.group(0))
            kind = git("cat-file", "-t", oid.decode("ascii") + "^{}").strip()
        if kind != b"commit":
            add("ref " + name + ":1", "non-commit ref requires review")
    commits = git("rev-list", "--all", *([head] if head else [])).splitlines()
    stats["commits"] = len(commits)
    for raw_sha in commits:
        sha = raw_sha.decode("ascii")
        raw = git("cat-file", "commit", sha).decode("utf-8", errors="replace")
        headers, _, message = raw.partition("\n\n")
        scan(sha + ":[commit]", raw)
        scan(sha + ":[message]", message)
        for role in ("author", "committer"):
            line = next((line for line in headers.splitlines() if line.startswith(role + " ")), "")
            identity = re.match(rf"{role} (.*) <([^<>]*)>", line)
            if not identity or identity.groups() != (CONFIG["git_name"], CONFIG["git_email"]):
                add(f"{sha}:[{role}]:1", role + " identity")
            if identity:
                scan(f"{sha}:[{role}]", identity.group(0))
        changes = git("diff-tree", "--root", "-m", "-r", "--raw", "-z", "--no-renames", "--no-commit-id", sha).split(b"\0")
        for i in range(0, len(changes) - 1, 2):
            header, filename = changes[i], os.fsdecode(changes[i + 1])
            oldmode, mode, old, oid, status = header.decode("ascii").split()
            label = sha + ":" + filename
            if status == "D":
                scan(label + " [path]", filename)
            else:
                scan_blob(label, filename, mode, oid, old if oldmode != ":160000" else None)
    if not bare:
        for name in git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
            if not name:
                continue
            filename = os.fsdecode(name)
            stats["untracked_files"] += 1
            scan(filename + " [path]", filename)
            file = repo / filename
            try:
                if file.is_symlink():
                    data = os.fsencode(os.readlink(file))
                elif file.is_file():
                    data = file.read_bytes()
                else:
                    add(filename + ":1", "untracked entry requires review")
                    continue
            except OSError:
                add(filename + ":1", "unreadable file; scan incomplete")
                continue
            scan_content(filename, filename, data)
    for artifact in artifacts or ():
        scan_artifact(artifact)
    return {"ok": not findings, "findings": findings, "stats": stats}
