"""Build a scanned, allowlisted public snapshot without copying private history."""
import contextlib
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import stat
import sys
import tempfile
import time

from factory.core import leaks, privacy
from factory.util import CONFIG, ROOT, CommandError, public_destinations as destinations, run

POLICY = pathlib.Path(__file__).with_name("public-export.json")
SNAPSHOT_MARKER = "Factory-Public-Snapshot: 1"
MAX_FILE_BYTES = 2 * 1024 * 1024


def policy():
    return json.loads(POLICY.read_text())


@contextlib.contextmanager
def phase(name):
    """Progress goes to stderr so a timeout names its stage; stdout stays one JSON result."""
    started = time.monotonic()
    print(f"public: {name} started", file=sys.stderr, flush=True)
    outcome = "failed"
    try:
        yield
        outcome = "finished"
    finally:
        print(f"public: {name} {outcome} after {time.monotonic() - started:.1f}s", file=sys.stderr, flush=True)


def public_path(name, rules):
    relative = pathlib.PurePosixPath(name)
    if (not relative.parts or relative.is_absolute() or name != relative.as_posix()
            or any(part in {"..", ".git"} for part in relative.parts)):
        return False
    generated = {"README.md", "factory.config.json", "factory/core/public-export.json", "public-export.json"}
    return (name in rules["files"] or name in rules.get("generated", {}) or name in generated
            or metrics_path(name))


def metrics_path(name):
    relative = pathlib.PurePosixPath(name)
    return (len(relative.parts) >= 2 and relative.parts[0] == "site" and name == relative.as_posix()
            and not any(part in {"..", ".git"} for part in relative.parts)
            and relative.suffix in {".html", ".css", ".js", ".json", ".svg"})


def regular_file(root, name):
    relative = pathlib.PurePosixPath(name)
    if (not relative.parts or relative.is_absolute() or name != relative.as_posix()
            or any(part in {"..", ".git"} for part in relative.parts)):
        raise ValueError("export paths must stay inside the source tree")
    root = pathlib.Path(root).resolve()
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"symlinks are not public export inputs: {name}")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
        raise ValueError(f"export input must be a bounded regular file: {name}")
    data = path.read_bytes()
    text = data.decode("utf-8")
    if "\0" in text:
        raise ValueError(f"binary input is not allowed: {name}")
    return text, bool(info.st_mode & 0o111)


def public_config(config):
    """Never copy local overrides, identities, approval quotes or arbitrary new keys."""
    repo, dashboard = destinations(config)
    since = dt.date.fromisoformat(config["public"].get("disclosure_since", "2026-11-20")).isoformat()
    result = {"github_user": "factory-owner", "git_name": "Contributor",
              "git_email": "contributor@users.noreply.github.com", "own_repositories": [],
              "repos_dir": "workspace/repos", "runs_dir": "runs", "ledger_file": "ledger/prs.json",
              "lessons_file": "lessons/LESSONS.md", "repo_renames": {}, "git_email_previous": [],
              "public": {"repository": repo, "dashboard_url": dashboard, "disclosure_since": since}}
    # Limits are numeric policy, not a channel for arbitrary text from local config.
    limits = config.get("limits", {})
    result["limits"] = {key: value for key, value in limits.items()
                        if re.fullmatch(r"[a-z_]+", key) and (value is None or type(value) in (int, float))}
    return result


def scrub(text, rules, config):
    for old, new in rules:
        text = text.replace(old, new)
    # Workstation paths and author identity are never useful in a reusable example.
    text = re.sub(r"/(?:Users|home)/[^/\s\"'`]+", "/home/user", text)
    for email in [config.get("git_email", ""), *config.get("git_email_previous", [])]:
        if email:
            text = text.replace(email, "contributor@users.noreply.github.com")
    for name in sorted({config.get("github_user", ""), config.get("git_name", "")}, key=len, reverse=True):
        if name:
            text = re.sub(rf"(?<![\w-]){re.escape(name)}(?![\w-])", "factory-owner", text)
    return text


def scan_text(name, text, reviewed=()):
    """Only exact, reviewed source lines can excuse scanner implementation literals."""
    lines = text.split("\n")
    findings = []
    for start, end, category, value in privacy.matches(text, name):
        line = text.count("\n", 0, start) + 1
        source = lines[line - 1]
        if "\n" not in text[start:end] and privacy.reviewed_source_line(source, category, reviewed):
            continue
        findings.append({"file": name, "line": line, "scanner": "privacy", "category": category})
    for message in leaks.scan(text=text, label="content"):
        match = re.search(r"at line (\d+)", message)
        findings.append({"file": name, "line": int(match[1]) if match else 1,
                         "scanner": "gitleaks", "category": message})
    return findings


def content_findings(name, text, rules, config):
    found = scan_text(name + " [path]", name)
    reviewed = rules.get("reviewed_source_lines", {}).get(name, [])
    found += scan_text(name, text, reviewed)
    forbidden = [*config.get("private_terms", []), *config.get("private_repos", [])]
    forbidden += [old for old, new in rules.get("scrub", []) if old != new]
    lines = text.split("\n")
    for term in forbidden:
        if not term:
            continue
        if re.search(re.escape(term), name, re.I):
            found.append({"file": name, "line": 1, "scanner": "plain", "category": "private path reference"})
        for match in re.finditer(re.escape(term), text, re.I):
            line = text.count("\n", 0, match.start()) + 1
            if "\n" not in match[0] and privacy.reviewed_source_line(lines[line - 1], "private reference", reviewed):
                continue
            found.append({"file": name, "line": line, "scanner": "plain", "category": "private reference"})
    return found


def scan_tree(tree, rules, config):
    tree = pathlib.Path(tree).resolve()
    found = []
    for path in sorted(tree.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        name = path.relative_to(tree).as_posix()
        if not public_path(name, rules):
            raise ValueError(f"unlisted public path: {name}")
        text, _ = regular_file(tree, name)
        found += content_findings(name, text, rules, config)
    return found


def write_file(tree, name, text, executable=False):
    relative = pathlib.PurePosixPath(name)
    if relative.is_absolute() or not relative.parts or any(p in {"..", ".git"} for p in relative.parts):
        raise ValueError("invalid public output path")
    path = tree / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)


def metrics_files(root, *, config, records_root=None, suite_report=None):
    """Metrics owns the public projection; raw records never enter the export."""
    module_path = root / "factory/core/metrics.py"
    if not module_path.exists():
        return {}, "not installed"
    from factory.core import metrics

    provider = getattr(metrics, "public_files", None)
    if provider is None:
        raise ValueError("installed metrics module has no public export adapter")
    files = provider(root, config=config, records_root=records_root, suite_report=suite_report)
    if not isinstance(files, dict) or not files or any(not isinstance(k, str) or not isinstance(v, str)
                                                     for k, v in files.items()):
        raise ValueError("metrics public_files must return a nonempty text-file mapping")
    for name in files:
        if not metrics_path(name):
            raise ValueError("metrics export may only supply site text assets")
    return files, "included"


def readme(template, repo, dashboard, files, metrics_status):
    tokens = re.compile(r"\{(as_of|opened|merged|closed|open|projects|first_opened|local_date|today_opened|today_merged|today_closed|sessions_total|sessions_clean|sessions_since)\}")
    if metrics_status == "not installed":
        template = re.sub(r'<p align="center">.*?</p>', lambda match: '' if 'site/' in match[0] else match[0], template, flags=re.S)
        template = '\n\n'.join(part for part in template.split('\n\n') if not tokens.search(part))
    elif tokens.search(template):
        data = json.loads(files["site/metrics.json"])
        dates = sorted(row["opened"] for row in data["pull_requests"] if row["opened"])
        values = {"as_of": data["cohort"]["as_of"], "first_opened": dates[0] if dates else "n/a",
                  "projects": data["population"]["projects"],
                  **{key: data["metrics"]["prs_" + key]["value"] for key in ("opened", "merged", "closed", "open")}}
        activity = data.get("activity") or {}
        date = activity.get("as_of_local_date")
        today = next((row for row in activity.get("days", []) if date and row.get("date") == date), {})
        values.update(local_date=date, today_opened=today.get("opened"),
                      today_merged=today.get("merged"), today_closed=today.get("closed_unmerged"))
        if any(key.startswith('sessions_') for key in tokens.findall(template)):
            sessions = data["metrics"]["shift_completion_rate"]
            if sessions["status"] == "not_tracked" or sessions["value"] is None or not sessions.get("since"):
                raise ValueError("README session summaries need a tracked value and since date")
            values.update(sessions_total=sessions["denominator"], sessions_clean=sessions["numerator"], sessions_since=sessions["since"])
        template = tokens.sub(lambda match: str(values[match[1]]) if values[match[1]] is not None else "n/a", template)
    return template.replace("{repository}", repo).replace("{dashboard_url}", dashboard)


def build(out, *, root=ROOT, config=None, rules=None, records_root=None, suite_report=None):
    config = CONFIG if config is None else config
    rules = policy() if rules is None else rules
    root = pathlib.Path(root).resolve()
    out = pathlib.Path(out).expanduser()
    if out.is_symlink():
        raise ValueError("export destination must not be a symlink")
    out = out.resolve()
    if out == root or root in out.parents or out in root.parents:
        raise ValueError("export destination must be separate from the private checkout")
    if out.exists():
        raise ValueError("export destination already exists; choose a new directory")
    repo, dashboard = destinations(config)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".public-export-", dir=out.parent) as temporary:
        tree = pathlib.Path(temporary) / "tree"
        tree.mkdir()
        for name in rules["files"]:
            text, executable = regular_file(root, name)
            write_file(tree, name, scrub(text, rules.get("scrub", []), config), executable)
        starter = public_config(config)
        starter["repo_renames"] = rules.get("repo_renames", {})
        write_file(tree, "factory.config.json", json.dumps(starter, indent=2) + "\n")
        files, metrics_status = metrics_files(root, config=config, records_root=records_root, suite_report=suite_report)
        template, _ = regular_file(root, rules["readme_template"])
        write_file(tree, "README.md", readme(template, repo, dashboard, files, metrics_status))
        for name, text in rules.get("generated", {}).items():
            write_file(tree, name, text)
        # The public policy contains no private name mappings or private inventory.
        clean_policy = {key: value for key, value in rules.items() if key not in {"scrub", "private_reasons"}}
        clean_policy["scrub"] = []
        write_file(tree, "factory/core/public-export.json", json.dumps(clean_policy, indent=2) + "\n")
        for name, text in files.items():
            if (tree / name).exists():
                raise ValueError("metrics export collides with an allowlisted path")
            write_file(tree, name, text)
        manifest = {"schema": 1, "metrics": metrics_status, "tests": rules["tests"],
                    "privacy": "Allowlisted files only; no private history or operational records.",
                    "reviewed_scanner_literals": rules.get("reviewed_source_lines", {})}
        write_file(tree, "public-export.json", json.dumps(manifest, indent=2) + "\n")
        findings = scan_tree(tree, rules, config)
        if findings:
            return {"ok": False, "findings": findings, "metrics": metrics_status}
        entries = [p for p in tree.rglob("*") if p.is_file()]
        result = {"ok": True, "files": len(entries), "bytes": sum(p.stat().st_size for p in entries),
                  "metrics": metrics_status, "findings": []}
        # Nothing appears at the destination until every file passes.
        tree.rename(out)
        return result


def git_environment():
    # An inherited GIT_DIR or alternate index must not redirect a disposable commit.
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def git(cwd, *args, env=None):
    return run(["git", "-C", str(cwd), *args], env=git_environment() if env is None else env).stdout.strip()


def validate_history(clone, rules, config):
    if git(clone, "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("public snapshot history must not be shallow")
    heads = git(clone, "rev-list", "--all").splitlines()
    if len(heads) > 1000:
        raise ValueError("public history exceeds the bounded snapshot scan")
    seen = set()
    for head in heads:
        message = git(clone, "show", "-s", "--format=%B", head)
        parents = git(clone, "rev-list", "--parents", "-n", "1", head).split()[1:]
        if SNAPSHOT_MARKER not in message.splitlines() or len(parents) > 1:
            raise ValueError("public clone contains non-snapshot history")
        if content_findings("commit", git(clone, "cat-file", "commit", head), rules, config):
            raise ValueError("public snapshot history contains a metadata scan finding")
        for entry in git(clone, "ls-tree", "-rz", head).split("\0"):
            if not entry:
                continue
            header, name = entry.split("\t", 1)
            mode, kind, oid = header.split()
            if not public_path(name, rules):
                raise ValueError(f"unlisted public path in snapshot history: {name}")
            if mode not in {"100644", "100755"} or kind != "blob":
                raise ValueError("public history contains a symlink or submodule")
            if (name, oid) in seen:
                continue
            seen.add((name, oid))
            if int(git(clone, "cat-file", "-s", oid)) > MAX_FILE_BYTES:
                raise ValueError("public history contains an oversized file")
            text = git(clone, "cat-file", "blob", oid)
            if "\0" in text or content_findings(name, text, rules, config):
                raise ValueError("public snapshot history contains a file scan finding")


def sync_tree(tree, checkout):
    """Replace a dedicated snapshot checkout, retaining only its Git metadata."""
    for path in checkout.iterdir():
        if path.name == ".git":
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    for path in tree.rglob("*"):
        if path.is_file() or path.is_symlink():
            name = path.relative_to(tree).as_posix()
            text, executable = regular_file(tree, name)
            write_file(checkout, name, text, executable)
    git(checkout, "add", "--all")


def snapshot_commit(checkout, config, *, allow_empty=False):
    message = "Update public factory snapshot\n\n" + SNAPSHOT_MARKER
    env = dict(git_environment(), GIT_AUTHOR_NAME=config["git_name"], GIT_AUTHOR_EMAIL=config["git_email"],
               GIT_COMMITTER_NAME=config["git_name"], GIT_COMMITTER_EMAIL=config["git_email"])
    git(checkout, "commit", *(["--allow-empty"] if allow_empty else []), "-m", message, env=env)
    return message


def validate_clone(clone, root, rules, config):
    if clone == root or root in clone.parents or clone in root.parents:
        raise ValueError("public clone must be separate from the private checkout")
    if not (clone / ".git").is_dir() or (clone / ".git").is_symlink():
        raise ValueError("public clone must be a normal non-bare Git clone")
    if pathlib.Path(git(clone, "rev-parse", "--show-toplevel")).resolve() != clone:
        raise ValueError("--clone must name the public clone root")
    if git(clone, "symbolic-ref", "--quiet", "HEAD") != "refs/heads/main":
        raise ValueError("public clone must be on main")
    if git(clone, "status", "--porcelain", "--untracked-files=all", "--ignored"):
        raise ValueError("public clone must have a clean worktree, including untracked and ignored files")
    repository, _ = destinations(config)
    url = f"https://github.com/{repository}"
    for args in (("config", "--get-all", "remote.origin.url"),
                 ("remote", "get-url", "--all", "origin"),
                 ("remote", "get-url", "--push", "--all", "origin")):
        if git(clone, *args) not in {url, url + ".git"}:
            raise ValueError("origin must point only to public.repository over HTTPS for fetch and push")
    validate_history(clone, rules, config)


def publish(clone, *, root=ROOT, config=None, rules=None, records_root=None, suite_report=None):
    """Refresh the persistent snapshot clone and publish only through the scanned wrapper."""
    config = CONFIG if config is None else config
    rules = policy() if rules is None else rules
    result = {"ok": False, "changed": False, "commit": None, "pushed": False, "findings": []}
    try:
        if not clone:
            raise ValueError("public publish requires --clone or public.clone in local config")
        clone = pathlib.Path(clone).expanduser().resolve()
        root = pathlib.Path(root).resolve()
        with phase("clone and history validation"):
            validate_clone(clone, root, rules, config)
        with tempfile.TemporaryDirectory(prefix="factory-public-export-") as temporary:
            tree = pathlib.Path(temporary).resolve() / "tree"
            with phase("export build and tree scan"):
                export = build(tree, root=root, config=config, rules=rules,
                               records_root=records_root, suite_report=suite_report)
            if not export["ok"]:
                result["findings"] = export["findings"]
                return result
            with phase("fetch and fetched-history validation"):
                git(clone, "fetch", "--prune", "--no-tags", "origin", "+refs/heads/*:refs/remotes/origin/*")
                validate_history(clone, rules, config)
            remote = git(clone, "for-each-ref", "--format=%(refname)", "refs/remotes/origin")
            if "refs/remotes/origin/main" in remote.splitlines():
                git(clone, "merge", "--ff-only", "refs/remotes/origin/main")
            elif remote:
                raise ValueError("nonempty public destination has no main branch")
            head = git(clone, "for-each-ref", "--format=%(objectname)", "refs/heads/main")
            result["commit"] = head or None
            sync_tree(tree, clone)
            if head and git(clone, "write-tree") == git(clone, "rev-parse", "HEAD^{tree}"):
                result["ok"] = True
                return result
            result["changed"] = True
            snapshot_commit(clone, config)
            result["commit"] = git(clone, "rev-parse", "HEAD")
            with phase("snapshot history validation"):
                validate_history(clone, rules, config)
            with phase("scanned push"):
                pushed = run([str(ROOT / "bin/factory"), "push", "-C", str(clone), "--", "origin", "main"],
                             check=False, env=git_environment())
            if pushed.returncode:
                result["findings"] = [privacy.redact(pushed.stderr or pushed.stdout or "scanned push failed")]
            else:
                result.update(ok=True, pushed=True)
    except (ValueError, OSError, CommandError) as error:
        result["findings"] = [privacy.redact(str(error))]
    return result


def snapshot_plan(tree, *, clone=None, config=None, rules=None):
    """Create a disposable local commit; never fetch GitHub, push, or change the source clone."""
    config = CONFIG if config is None else config
    rules = policy() if rules is None else rules
    from factory.core import publication

    tree = pathlib.Path(tree).resolve()
    repository, _ = destinations(config)
    with phase("snapshot input tree scan"):
        if scan_tree(tree, rules, config):
            raise ValueError("snapshot input has scan findings")
    with tempfile.TemporaryDirectory(prefix="factory-public-plan-") as temporary:
        checkout = pathlib.Path(temporary) / "snapshot"
        # Clone into a disposable bare destination too: even push preflight never addresses the source.
        destination = pathlib.Path(temporary) / "destination.git"
        if clone is not None:
            clone = pathlib.Path(clone).resolve()
            with phase("source clone history validation"):
                validate_history(clone, rules, config)
            run(["git", "clone", "--bare", "--no-hardlinks", "--no-local", str(clone), str(destination)],
                env=git_environment())
        else:
            run(["git", "init", "--bare", "--initial-branch=main", str(destination)], env=git_environment())
        refs = publication.remote_refs(str(destination), destination, env=git_environment())
        parent = refs.get("refs/heads/main")
        if refs and parent is None:
            raise ValueError("nonempty public destination has no main branch")
        run(["git", "clone", "--no-checkout", "--no-local", str(destination), str(checkout)], env=git_environment())
        if parent:
            git(checkout, "checkout", "-B", "main", parent)
        else:
            git(checkout, "checkout", "--orphan", "main")
        sync_tree(tree, checkout)
        message = snapshot_commit(checkout, config, allow_empty=True)
        with phase("snapshot history validation"):
            validate_history(checkout, rules, config)
        head = git(checkout, "rev-parse", "HEAD")
        refspec = "HEAD:refs/heads/main"
        url = f"https://github.com/{repository}.git"
        local_plan = publication.planned_push([str(destination), refspec], checkout, env=git_environment())
        # Reuse Git's local fast-forward proof and advertisement under the intended public URL.
        # check_push scans the actual outgoing commits; it does not contact that URL with these inputs.
        plan = [(url, flag, source, dest, summary) for _, flag, source, dest, summary in local_plan]
        with phase("push preflight"):
            findings = publication.check_push([url, refspec], checkout, plan=plan, advertised={url: refs})
        if findings:
            raise ValueError("public push preflight failed: " + "; ".join(findings))
        return {"ok": True, "dry_run": True, "repository": repository, "head": head,
                "push_preflight": {"ok": True, "destination": "disposable local bare repository",
                                   "scanner": "publication.check_push", "findings": []},
                "tree": git(checkout, "rev-parse", "HEAD^{tree}"), "parent": parent,
                "message": message, "commits": int(git(checkout, "rev-list", "--count", "HEAD")),
                "push_argv": ["bin/factory", "push", "-C", "<public-snapshot-clone>", "--",
                              f"https://github.com/{repository}.git", "HEAD:refs/heads/main"],
                "publication": "Not executed. The scanned push wrapper remains mandatory; no raw git push.",
                "commit_retained": False}


def cmd_public(args):
    try:
        with leaks.reuse():
            result = public_result(args)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    except (ValueError, OSError, CommandError) as error:
        raise SystemExit(privacy.redact(f"public {args.action}: {error}")) from None


def public_result(args):
    options = {key: getattr(args, key) for key in ("records_root", "suite_report") if getattr(args, key, None)}
    if args.action == "export":
        return build(args.out, **options)
    expected, _ = destinations(CONFIG)
    if args.repo and args.repo != expected:
        raise ValueError("--repo must match public.repository in factory.config.json")
    if not args.dry_run:
        return publish(args.clone or CONFIG.get("public", {}).get("clone"), **options)
    with tempfile.TemporaryDirectory(prefix="factory-public-export-") as temporary:
        tree = pathlib.Path(temporary).resolve() / "tree"
        with phase("export build and tree scan"):
            result = build(tree, **options)
        if not result["ok"]:
            return result
        # A dry run plans against an explicit --clone only; without one it plans an initial snapshot.
        plan = snapshot_plan(tree, clone=args.clone)
        plan["export"] = result
        return plan


def register(sub):
    command = sub.add_parser("public", help="scanned public export and snapshot publication")
    actions = command.add_subparsers(dest="action", required=True)
    export = actions.add_parser("export", help="write a new, scanned allowlist-only public tree")
    export.add_argument("--out", type=pathlib.Path, required=True)
    export.add_argument("--records-root", type=pathlib.Path, help="read metrics records from another checkout without updating it")
    export.add_argument("--suite-report", type=pathlib.Path, help="full-suite JSON for the exported metrics site")
    export.set_defaults(fn=cmd_public)
    publish = actions.add_parser("publish", help="refresh a public snapshot clone through the scanned push wrapper")
    publish.add_argument("--repo", help="must equal public.repository")
    publish.add_argument("--clone", type=pathlib.Path, help="public clone; defaults to public.clone for a real publication")
    publish.add_argument("--dry-run", action="store_true", help="offline disposable plan; no pushes or source-clone changes")
    publish.add_argument("--records-root", type=pathlib.Path, help="read metrics records from another checkout")
    publish.add_argument("--suite-report", type=pathlib.Path, help="reuse a full-suite JSON report for the metrics site")
    publish.set_defaults(fn=cmd_public)
