"""Shared configuration, filesystem, command and item helpers."""
import datetime as dt
import json
import os
import pathlib
import re
import stat
import subprocess
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_config(root=None):
    root = pathlib.Path(root) if root is not None else ROOT
    cfg = json.loads((root / "factory.config.json").read_text())
    local = root / ".factory.local.json"
    if local.exists():
        override = json.loads(local.read_text())
        limits = {**cfg.get("limits", {}), **override.pop("limits", {})}
        cfg.update(override)
        cfg["limits"] = limits
    return cfg


CONFIG = load_config()


def public_destinations(config):
    public = config.get("public", {})
    if not isinstance(public, dict):
        raise ValueError("configure public destinations as an object")
    repo, dashboard = public.get("repository", ""), public.get("dashboard_url", "")
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*", repo, re.ASCII):
        raise ValueError("configure public.repository as owner/repository")
    owner, name = repo.split("/")
    if dashboard != f"https://{owner}.github.io/{name}/":
        raise ValueError("public.dashboard_url must be the repository's GitHub Pages URL")
    return repo, dashboard


def path_from_config(key):
    p = pathlib.Path(CONFIG[key]).expanduser()
    return p if p.is_absolute() else ROOT / p


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t=None):
    return (t or now()).isoformat(timespec="seconds")


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def hours_since(s):
    return (now() - parse_iso(s)).total_seconds() / 3600


class CommandError(RuntimeError):
    pass


def run(cmd, cwd=None, check=True, input=None, env=None, timeout=None):
    try:
        proc = subprocess.run(cmd, cwd=cwd, input=input, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CommandError(f"{' '.join(cmd)} timed out after {timeout}s") from None
    if check and proc.returncode != 0:
        raise CommandError(f"{' '.join(cmd)} failed ({proc.returncode}): {proc.stderr.strip()[:2000]}")
    return proc


def git(repo, *args, check=True):
    return run(["git", "-C", str(repo), *args], check=check).stdout.strip()


# Reads only: a write that times out locally may still have landed on GitHub.
GH_READ_TIMEOUT = 120


def gh_json(*args, timeout=None):
    out = run(["gh", *args], timeout=timeout).stdout
    return json.loads(out) if out.strip() else None


def gh_paginate(endpoint, timeout=GH_READ_TIMEOUT):
    out = run(["gh", "api", "--paginate", endpoint, "--jq", ".[]"], timeout=timeout).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def read_json(path, default=None):
    path = pathlib.Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path, data):
    """Replace complete JSON atomically, following symlinks and retaining existing permissions."""
    path = pathlib.Path(path)
    if path.is_symlink():
        path = path.resolve()
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    temporary = None
    try:
        candidate = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        temporary = candidate
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


GITHUB_URL = re.compile(r"^https?://github\.com/([^/]+/[^/]+)/(?:issues|pull|discussions)/(\d+)(?:[/?#].*)?$")


def normalize_ref(ref):
    match = GITHUB_URL.match(ref.strip())
    return f"{match[1]}#{match[2]}" if match else ref


def split_ref(ref):
    repo, _, num = normalize_ref(ref).partition("#")
    if "/" not in repo or not num.isdigit():
        raise SystemExit(f"expected owner/repo#number, got {ref!r}")
    return repo, int(num)


def issue_number(ref, repo):
    text = str(ref or "").strip()
    match = re.fullmatch(r"#?([1-9]\d*)", text)
    if not match:
        match = re.fullmatch(rf"{re.escape(repo)}#([1-9]\d*)", text, re.I)
    if not match:
        match = re.fullmatch(rf"https://github\.com/{re.escape(repo)}/issues/([1-9]\d*)/?(?:#[\w-]+)?", text, re.I)
    if not match:
        raise ValueError(f"expected an issue number or issue URL in {repo}, got {ref!r}")
    return int(match[1])


def clone_dir(repo):
    return path_from_config("repos_dir") / repo.replace("/", "__")


class Item:
    """A unit of work: one directory holding item.json plus its evidence files."""

    def __init__(self, path):
        self.dir = pathlib.Path(path).resolve()
        self.meta_path = self.dir / "item.json"
        if not self.meta_path.exists():
            raise SystemExit(f"{self.meta_path} not found")
        self.meta = read_json(self.meta_path)

    def save(self):
        write_json(self.meta_path, self.meta)

    def file(self, name):
        return self.dir / name

    @property
    def schema(self):
        return int(self.meta.get("schema", 1))

    @property
    def clone(self):
        return pathlib.Path(self.meta.get("clone") or clone_dir(self.meta["repo"])).expanduser()

    @property
    def base_ref(self):
        """The ref this item's own diff is measured against: the branch it is stacked on, else the upstream base."""
        return self.meta.get("stacked_on") or f"origin/{self.meta.get('base', 'main')}"

    def head_sha(self):
        from factory.core.clones import released

        branch = self.meta["branch"]
        exists = self.clone.exists() and run(
            ["git", "-C", str(self.clone), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False,
        ).returncode == 0
        if not exists:
            record = released(self)
            if record and record.get("head"):
                return record["head"]
        return git(self.clone, "rev-parse", branch)

    def merge_base(self):
        from factory.core.clones import released

        branch = self.meta["branch"]
        exists = self.clone.exists() and run(
            ["git", "-C", str(self.clone), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False,
        ).returncode == 0
        if not exists:
            record = released(self)
            if record and record.get("base"):
                return record["base"]
            raise CommandError(f"item branch is unavailable; run bin/factory clone ensure --item {self.dir}")
        return git(self.clone, "merge-base", self.base_ref, branch)


def global_hooks_drift():
    """Files in the machine's global hooks directory that differ from the recorded baseline (L-035)."""
    import hashlib

    baseline = CONFIG.get("global_hooks_baseline") or {}
    path = run(["git", "config", "--global", "--get", "core.hooksPath"], check=False).stdout.strip()
    if not baseline or not path:
        return []
    folder = pathlib.Path(path).expanduser()
    drift = []
    for name, digest in baseline.items():
        f = folder / name
        if not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest() != digest:
            drift.append(name)
    return drift


def local_hooks_overrides():
    """Clones whose own config points core.hooksPath away from the machine's hooks, e.g. after a
    husky or lefthook install that ran without HUSKY=0 LEFTHOOK=0 (L-035)."""
    candidates = list(path_from_config("repos_dir").glob("*/.git")) + list((ROOT / "workspace").glob("*/*/.git"))
    seen, found = set(), []
    for dotgit in candidates:
        clone = dotgit.parent
        common = run(["git", "-C", str(clone), "rev-parse", "--git-common-dir"], check=False).stdout.strip()
        common = str((clone / common).resolve()) if common else str(clone)
        if common in seen:
            continue
        seen.add(common)
        value = run(["git", "-C", str(clone), "config", "--local", "--get", "core.hooksPath"], check=False).stdout.strip()
        if value:
            found.append((clone, value))
    return found
