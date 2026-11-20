"""Release idle checkouts without losing local commits, and restore them on demand.

Recovery bundles are deliberately thin and require origin's history at restore time.
"""
import json
import os
import pathlib
import re
import shutil

from factory.core import clean, ledger
from factory.util import CONFIG, CommandError, Item, clone_dir, git, iso, now, path_from_config, read_json, run, write_json


def released(item):
    record = read_json(item.meta_path).get("released")
    return record if isinstance(record, dict) else None


def _items():
    statuses = {pathlib.Path(row["item"]).resolve(): row["status"] for row in ledger.load() if row.get("item")}
    runs = path_from_config("runs_dir")
    uses = {}
    for meta in sorted({*runs.glob("*/*/item.json"), *runs.glob("*/*/*/item.json")}):
        item = Item(meta.parent)
        uses.setdefault(item.clone.resolve(), []).append((item, statuses.get(item.dir, item.meta.get("status"))))
    return uses


def _heads(checkout):
    lines = git(checkout, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/").splitlines()
    return {ref.removeprefix("refs/heads/"): sha for ref, sha in (line.split(" ", 1) for line in lines)}


def _local_refs(checkout):
    refs = {}
    for line in git(checkout, "for-each-ref", "--format=%(refname) %(objectname)").splitlines():
        ref, sha = line.split(" ", 1)
        if ref != "refs/stash" and not ref.startswith("refs/remotes/"):
            refs[ref] = sha
    return refs


def _git_stdin(checkout, *args, lines):
    """Run git with revisions on standard input: a clone can hold tens of thousands of tags, more than an argument list takes."""
    return run(["git", "-C", str(checkout), *args], input="".join(f"{line}\n" for line in lines)).stdout.strip()


def _bundle_refs(checkout, refs):
    if not refs:
        return {}
    names = list(refs)
    rows = _git_stdin(checkout, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                      lines=[f"{ref}^{{commit}}" for ref in names]).splitlines()
    if len(rows) != len(names):
        raise CommandError("cannot resolve the local refs to commits; repair the checkout before releasing")
    tips = {}
    for ref, row in zip(names, rows):
        tip, _, kind = row.partition(" ")
        if kind != "commit":
            raise CommandError(f"local ref {ref} does not point to a commit; back it up and remove it before releasing")
        tips[ref] = tip
    unique = set(_git_stdin(checkout, "rev-list", "--stdin", "--not", "--remotes=origin",
                            lines=sorted(set(tips.values()))).splitlines())
    return {ref: refs[ref] for ref, tip in tips.items() if tip in unique}


def _dirty(checkout):
    return run(["git", "--no-optional-locks", "-C", str(checkout), "status", "--porcelain",
                "--untracked-files=all", "--ignore-submodules=none"]).stdout.strip()


def _submodule_reasons(checkout, prefix=pathlib.Path(), fetch=False):
    """Why a submodule blocks a release; with fetch, its remotes are refreshed first so stale refs prove nothing."""
    reasons = []
    entries = git(checkout, "ls-files", "--stage", "-z").split("\0")
    paths = set()
    for entry in entries:
        if entry.startswith("160000 "):
            paths.add(entry.split("\t", 1)[1])
    for path in sorted(paths):
        child = checkout / path
        if not (child / ".git").exists():
            continue
        name = prefix / path
        try:
            if fetch:
                git(child, "fetch", "--all", "--prune")
            if _dirty(child):
                reasons.append(f"submodule {name} has uncommitted or untracked files; commit or move them before releasing")
            if clean.stashes(child):
                reasons.append(f"submodule {name} has stash entries; apply and commit them or move them before releasing")
            if _git_stdin(child, "rev-list", "--max-count=1", "--stdin", "--not", "--remotes",
                          lines=["HEAD", *_local_refs(child)]):
                reasons.append(f"submodule {name} has commits no remote ref reaches; push or back them up before releasing")
            reasons.extend(_submodule_reasons(child, name, fetch))
        except CommandError as error:
            raise CommandError(f"cannot verify submodule {name}: {error}; repair or back it up before releasing") from None
    return reasons


def _remotes(checkout):
    remotes = {}
    for name in git(checkout, "remote").splitlines():
        entry = {"url": git(checkout, "remote", "get-url", name)}
        pushurl = git(checkout, "config", "--get", f"remote.{name}.pushurl", check=False)
        if pushurl:
            entry["pushurl"] = pushurl
        remotes[name] = entry
    if "origin" not in remotes:
        raise CommandError("no origin remote; add a fetchable origin before releasing")
    return remotes


def _why(checkout, submodules=True):
    reasons = []
    try:
        if _restore_marker(checkout).exists():
            reasons.append("incomplete clone restore; finish clone ensure for the recorded item or move the checkout aside before releasing")
        if _dirty(checkout):
            reasons.append("uncommitted or untracked files; commit or move them before releasing")
        if submodules:
            reasons.extend(_submodule_reasons(checkout))
        if clean.stashes(checkout):
            reasons.append("stash entries; apply and commit them or move them before releasing")
        if (checkout / ".git").is_dir():
            worktrees = git(checkout, "worktree", "list", "--porcelain").splitlines()
            if len([line for line in worktrees if line.startswith("worktree ")]) > 1:
                reasons.append("linked worktrees depend on this clone; release or remove them first")
        head = git(checkout, "rev-parse", "--verify", "HEAD", check=False)
        if head and _git_stdin(checkout, "rev-list", "--max-count=1", "--stdin", "--not", "--remotes=origin",
                               lines=[head, *(f"^{ref}" for ref in _local_refs(checkout))]):
            reasons.append("detached commits have no local ref; create a branch for them before releasing")
        _remotes(checkout)
    except CommandError as error:
        raise CommandError(f"cannot verify checkout: {error}; repair it before releasing") from None
    return reasons


def _archive(checkout):
    if CONFIG.get("archive_dir"):
        archive = path_from_config("archive_dir")
    else:
        archive = path_from_config("repos_dir").parent / "archive"
    archive = archive.resolve()
    if any(archive.is_relative_to(path) for path in clean.checkouts()):
        raise CommandError("archive_dir is inside a managed checkout; move it outside before releasing")
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    return archive / f"{checkout.name}-{stamp}.bundle"


def _bundle(checkout, destination, refs):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb"):
        pass
    try:
        _git_stdin(checkout, "bundle", "create", str(destination), "--stdin", "--not", "--remotes=origin", lines=refs)
        git(checkout, "bundle", "verify", str(destination))
        entries = git(checkout, "bundle", "list-heads", str(destination)).splitlines()
        saved = {ref: sha for sha, ref in (entry.split(" ", 1) for entry in entries)}
        for ref, tip in refs.items():
            if saved.get(ref) != tip:
                raise CommandError(f"bundle did not preserve the tip of {ref}; keep the checkout and retry")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _release_plan(checkout, items):
    heads = _heads(checkout)
    remotes = _remotes(checkout)
    for item in items:
        if item.meta_path.resolve().is_relative_to(checkout):
            raise CommandError(f"item metadata is inside the checkout; move {item.dir} before releasing")
    refs = _local_refs(checkout)
    saved_refs = _bundle_refs(checkout, refs)
    bundle = _archive(checkout) if saved_refs else None
    if bundle and bundle.exists():
        raise CommandError(f"bundle already exists at {bundle}; keep it and retry after the next second")
    return heads, remotes, refs, saved_refs, bundle


def release(checkout, dry_run=False):
    checkout = pathlib.Path(checkout).expanduser().resolve()
    report = {"checkout": str(checkout), "status": "protected", "released": False, "why": [], "bytes": 0, "bundle": None, "items": []}
    if dry_run:
        report.update(dry_run=True, would_release=False)
    try:
        if checkout not in clean.checkouts():
            raise CommandError("not a managed checkout; select a checkout under repos_dir or workspace")
        items = [item for item, _ in _items().get(checkout, [])]
        report["items"] = [str(item.dir) for item in items]
        report["why"] = _why(checkout, submodules=dry_run)
        if report["why"]:
            if not dry_run:
                report["why"].extend(_submodule_reasons(checkout))
            return report
        if not dry_run:
            git(checkout, "fetch", "--all", "--prune")
            report["why"] = _submodule_reasons(checkout, fetch=True)
            if report["why"]:
                return report
        heads, remotes, refs, saved_refs, bundle = _release_plan(checkout, items)
        if dry_run:
            report.update(status="would_release", would_release=True, why=["would fetch"], bundle=str(bundle) if bundle else None)
            return report
        if bundle:
            _bundle(checkout, bundle, saved_refs)
            report["bundle"] = str(bundle)
        report["why"] = _why(checkout)
        if _local_refs(checkout) != refs or _remotes(checkout) != remotes:
            report["why"].append("local refs or remotes changed during release; stop writers and retry")
        if report["why"]:
            return report
        records = []
        at = iso()
        for item in items:
            branch = item.meta.get("branch")
            head = heads.get(branch)
            if head is None and released(item) is not None:
                # No branch was restored, including headless scout items; keep their original release record.
                continue
            base = None
            if head:
                base = git(checkout, "merge-base", item.base_ref, f"refs/heads/{branch}", check=False) or None
            record = {"at": at, "branch": branch, "head": head, "base": base,
                      "origin": remotes["origin"]["url"], "remotes": remotes}
            if f"refs/heads/{branch}" in saved_refs:
                record["bundle"] = str(bundle)
            records.append((item, record))
        for item, record in records:
            meta = read_json(item.meta_path)
            if meta != item.meta:
                raise CommandError(f"item changed during release; retry after work on {item.dir} stops")
            write_json(item.meta_path, {**meta, "released": record})
        report["why"] = _why(checkout)
        if _local_refs(checkout) != refs or _remotes(checkout) != remotes:
            report["why"].append("local refs or remotes changed during release; stop writers and retry")
        if report["why"]:
            return report
        freed = clean.size(checkout)
        if (checkout / ".git").is_file():
            common = git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
            git(common, "worktree", "remove", str(checkout))
        else:
            shutil.rmtree(checkout)
        report.update(status="released", released=True, bytes=freed)
    except (CommandError, OSError, ValueError) as error:
        report["status"] = "error"
        report["why"].append(f"release stopped: {error}; keep the checkout and resolve this before retrying")
    return report


def _restore_marker(checkout):
    directory = git(checkout, "rev-parse", "--absolute-git-dir")
    return pathlib.Path(directory) / "factory-restoring"


def _marker_state(checkout):
    """What an unfinished restore of this clone must reach: {item, branch, head}; {} when the marker says nothing."""
    try:
        state = json.loads(_restore_marker(checkout).read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _clone(checkout, origin, remotes, restoring=None):
    parent = checkout.parent
    while not parent.exists():
        parent = parent.parent
    minimum = CONFIG.get("limits", {}).get("min_clone_free_gib", 1)
    free = shutil.disk_usage(parent).free / 2**30
    if free < minimum:
        raise CommandError(f"{free:.2f} GiB free; cloning needs at least {minimum:g} GiB; free disk space and retry")
    checkout.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--filter=blob:none", origin, str(checkout)])
    if restoring is not None:
        try:
            with (checkout / ".git" / "factory-restoring").open("x") as marker:
                marker.write(json.dumps(restoring))
        except FileExistsError:
            pass
    _configure(checkout, remotes)


def _configure(checkout, remotes):
    git(checkout, "config", "user.name", CONFIG["git_name"])
    git(checkout, "config", "user.email", CONFIG["git_email"])
    for name, remote in remotes.items():
        if name != "origin" and not git(checkout, "remote", "get-url", name, check=False):
            git(checkout, "remote", "add", name, remote["url"])
        if remote.get("pushurl"):
            git(checkout, "remote", "set-url", "--push", name, remote["pushurl"])


def _present(checkout, head):
    result = run(["git", "-C", str(checkout), "cat-file", "-e", f"{head}^{{commit}}"],
                 check=False, env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})
    return result.returncode == 0


def _fetch_head(checkout, source, ref, head, tried):
    result = run(["git", "-C", str(checkout), "fetch", source, ref], check=False)
    if result.returncode:
        tried.append(f"{source} {ref}: failed ({result.returncode}) {result.stderr.strip()}")
        return False
    fetched = git(checkout, "rev-parse", "FETCH_HEAD")
    tried.append(f"{source} {ref}: returned {fetched}")
    if fetched != head:
        return False
    if not _present(checkout, head):
        tried[-1] += "; commit object is missing"
        return False
    return True


def _obtain(item, record, branch):
    head = record["head"]
    tried = []

    def failure(reason):
        return CommandError(f"{item.dir}: expected head {head}; {reason}; sources tried: {'; '.join(tried)}; restore a source and retry bin/factory clone ensure --item {item.dir}")

    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head):
        raise failure("invalid recorded commit")
    if _present(item.clone, head):
        return
    tried.append("local objects: recorded commit is missing")
    remotes = record.get("remotes", {})
    for ref in (f"refs/heads/{branch}", head):
        for remote in remotes:
            if _fetch_head(item.clone, remote, ref, head, tried):
                return
    if record.get("bundle"):
        bundle = record["bundle"]
        result = run(["git", "-C", str(item.clone), "bundle", "verify", bundle], check=False,
                     env={**os.environ, "LC_ALL": "C"})
        if result.returncode:
            detail = result.stderr.strip()
            if "prerequisite commits" in detail:
                tried.append(f"{bundle}: bundle's prerequisite commits are missing from origin; {detail}")
            else:
                tried.append(f"{bundle}: verification failed ({result.returncode}) {detail}")
        elif _fetch_head(item.clone, bundle, f"refs/heads/{branch}", head, tried):
            return
    raise failure("commit cannot be obtained")


def _finish(item, created, branch, expected=None):
    head = _heads(item.clone).get(branch)
    if expected and head != expected:
        raise CommandError(f"{item.dir}: expected head {expected}, but branch now returns {head}; keep the recovery record and move the incomplete clone aside before retrying")
    meta = read_json(item.meta_path)
    if "released" in meta:
        del meta["released"]
        write_json(item.meta_path, meta)
    item.meta = meta
    _restore_marker(item.clone).unlink(missing_ok=True)
    return {"item": str(item.dir), "checkout": str(item.clone), "created": created, "branch": branch, "head": head}


def ensure(item):
    branch = item.meta.get("branch")
    created = not item.clone.exists()
    restoring = False
    record = released(item) or {}
    if not created:
        if not (item.clone / ".git").exists():
            raise CommandError(f"{item.clone} is not a checkout; move it aside before ensuring {item.dir}")
        restoring = _restore_marker(item.clone).exists()
        if not restoring and branch in _heads(item.clone):
            return _finish(item, False, branch)
    if record.get("branch") and record["branch"] != branch:
        raise CommandError(f"{item.dir}: item branch differs from its release record; restore the recorded branch name before ensuring")
    expected = record.get("head")
    if restoring:
        expected = _unfinished_restore(item, record, branch)
    if created:
        origin = record.get("origin") or f"https://github.com/{item.meta['repo']}.git"
        state = {"item": str(item.dir), "branch": branch, "head": expected, "released": bool(record)}
        _clone(item.clone, origin, record.get("remotes", {}), restoring=state)
    elif restoring:
        _configure(item.clone, record.get("remotes", {}))
    if expected:
        if not branch:
            raise CommandError(f"{item.dir}: recorded head {expected} has no branch; restore the branch name before ensuring")
        local = _heads(item.clone).get(branch)
        if local and local != expected:
            raise CommandError(f"{item.dir}: expected head {expected}, but local branch is {local}; move the incomplete clone aside and retry from the recorded sources")
        if not local:
            if not record.get("head"):
                raise CommandError(f"{item.dir}: the clone is an unfinished restore that must reach {expected}, but item.json has no release record to restore it from; put the released block back or move the incomplete clone aside")
            _obtain(item, record, branch)
            git(item.clone, "branch", branch, expected)
    if (created or restoring) and branch in _heads(item.clone):
        git(item.clone, "checkout", branch)
    return _finish(item, created, branch, expected)


def _unfinished_restore(item, record, branch):
    """The commit an unfinished restore of this clone must reach for this item, from the record or the marker."""
    state = _marker_state(item.clone)
    owner = state.get("item")
    if owner and pathlib.Path(owner).expanduser().resolve() != item.dir:
        raise CommandError(f"{item.clone} is still being restored for {owner}; run bin/factory clone ensure --item {owner} first")
    if record.get("head"):
        return record["head"]
    if not owner:
        raise CommandError(f"{item.dir}: {item.clone} is an unfinished restore with no release record and no recorded target; move the incomplete clone aside and run clone ensure again")
    if state.get("released") and not record and not state.get("head"):
        raise CommandError(f"{item.dir}: the unfinished restore started from a release record that item.json no longer has; put the released block back or move the incomplete clone aside")
    if state.get("branch") != branch:
        raise CommandError(f"{item.dir}: the unfinished restore was started for branch {state.get('branch')}, not {branch}; move the incomplete clone aside before ensuring")
    return state.get("head")


def ensure_repo(repo):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise CommandError("expected owner/repo; provide a repository name, not a URL or path")
    checkout = clone_dir(repo)
    if not checkout.exists():
        _clone(checkout, f"https://github.com/{repo}.git", {})
    elif not (checkout / ".git").exists():
        raise CommandError(f"{checkout} is not a checkout; move it aside before ensuring the repository")
    return checkout


def status():
    uses = _items()
    rows = []
    for checkout in clean.checkouts():
        try:
            why = _why(checkout)
            if not why:
                _release_plan(checkout, [item for item, _ in uses.get(checkout, [])])
        except (CommandError, OSError, ValueError) as error:
            why = [str(error)]
        rows.append({"path": str(checkout), "size": clean.size(checkout),
                     "items": [{"item": str(item.dir), "status": state} for item, state in uses.get(checkout, [])],
                     "releasable": not why, "why": why})
    return rows


def release_idle(active, dry_run=False):
    active = {pathlib.Path(path).resolve() for path in active}
    uses = _items()
    reports = []
    checkouts = sorted(clean.checkouts(), key=lambda path: (not (path / ".git").is_file(), str(path)))
    for checkout in checkouts:
        if any(item.dir in active for item, _ in uses.get(checkout, [])):
            continue
        reports.append(release(checkout, dry_run=dry_run))
    return {"reports": reports, "bytes": sum(report["bytes"] for report in reports)}


def _command(args):
    try:
        if args.clone_action == "status":
            result = status()
        elif args.clone_action == "ensure":
            if args.item:
                result = ensure(Item(args.item))
            else:
                result = {"repo": args.repo, "checkout": str(ensure_repo(args.repo))}
        elif args.idle:
            from factory.core import nextaction

            code_items = getattr(nextaction, "code_items", None)
            if not callable(code_items):
                raise CommandError("loop integration is not installed; install nextaction.code_items before releasing idle checkouts")
            result = release_idle(code_items(), dry_run=args.dry_run)
        else:
            result = release(Item(args.item).clone, dry_run=args.dry_run)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.clone_action == "release":
            reports = result.get("reports", [result])
            if args.idle:
                return int(any(report["status"] == "error" for report in reports))
            return int(any(not (report["released"] or report.get("would_release")) for report in reports))
        return 0
    except (CommandError, OSError, ValueError, SystemExit) as error:
        print(json.dumps({"error": str(error)}, indent=2, ensure_ascii=False))
        return 1


def register(sub):
    group = sub.add_parser("clone", help="release idle checkouts and restore exact item branches")
    actions = group.add_subparsers(dest="clone_action", required=True)
    actions.add_parser("status", help="show local release checks; release also verifies remotes").set_defaults(fn=_command)
    ensure_parser = actions.add_parser("ensure", help="restore an item checkout or clone a repository")
    target = ensure_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--item")
    target.add_argument("--repo")
    ensure_parser.set_defaults(fn=_command)
    release_parser = actions.add_parser("release", help="archive unpushed branches and remove a safe checkout")
    target = release_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--item")
    target.add_argument("--idle", action="store_true")
    release_parser.add_argument("--dry-run", action="store_true")
    release_parser.set_defaults(fn=_command)
