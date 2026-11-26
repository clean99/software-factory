"""Command-line entry points for contribution preparation, review and release."""
import argparse
import datetime as dt
import json
import re
import shutil
import sys

from factory.core import clones
from factory.util import CONFIG, ROOT, CommandError, GH_READ_TIMEOUT, Item, clone_dir, gh_json, git, iso, path_from_config, read_json, normalize_ref, run, split_ref, write_json


def emit(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_doctor(a):
    ok = True

    def line(good, what):
        nonlocal ok
        ok &= good
        print(f"[{'ok' if good else 'FAIL'}] {what}")

    for tool in ("git", "gh", "python3", "gitleaks"):
        line(bool(shutil.which(tool)), f"{tool} on PATH")
    try:
        login = gh_json("api", "user")["login"]
        line(login.lower() == CONFIG["github_user"].lower(), f"gh authenticated as {login} (expected {CONFIG['github_user']})")
    except CommandError as e:
        line(False, f"gh auth: {e}")
    repos = path_from_config("repos_dir")
    repos.mkdir(parents=True, exist_ok=True)
    line(repos.is_dir(), f"repos dir {repos}")
    line(bool(shutil.which("node")), "node on PATH (most target repos need it)")
    line((ROOT / ".claude" / "settings.json").exists(), "Claude Code stop hook wired (.claude/settings.json)")
    hooks = run(["git", "-C", str(ROOT), "config", "core.hooksPath"], check=False).stdout.strip()
    line(hooks == ".githooks", "git core.hooksPath = .githooks (size guard on commits)")
    from factory.util import global_hooks_drift, local_hooks_overrides

    drift = global_hooks_drift()
    line(not drift, "machine global git hooks match the baseline" + (f" (changed: {', '.join(drift)})" if drift else ""))
    overrides = local_hooks_overrides()
    line(not overrides, "no clone overrides core.hooksPath" + "".join(f"\n      {c} -> {v}" for c, v in overrides))
    return 0 if ok else 1


def cmd_health(a):
    from factory.oss import health

    for repo in a.repos:
        if a.responsiveness:
            emit(health.responsiveness(repo, refresh=a.refresh))
        else:
            emit(health.run(repo, a.days, CONFIG["github_user"]))


def cmd_retro(a):
    from factory.oss import retro

    emit(retro.run(CONFIG["github_user"], a.since))


def cmd_watch(a):
    from factory.oss import watch

    try:
        report = watch.run(repos=a.repo, since=a.since, dry_run=a.dry_run)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    for line in watch.summary(report):
        print(line)
    return 1 if any(row["status"] == "error" for row in report["repos"]) else 0


def cmd_policy(a):
    from factory.oss import policy

    if a.note and not a.endorse:
        raise SystemExit("--note goes with --endorse")
    result = policy.refresh(a.repo)
    if a.endorse:
        try:
            policy.endorse(result, a.note)
        except ValueError as error:
            raise SystemExit(str(error)) from None
    result["owner_endorsed"] = policy.endorsed(result)
    result["automation_blockers"] = policy.blockers(result)
    emit(result)
    return 2 if result["automation_blockers"] else 0


def cmd_qualify(a):
    from factory.oss import policy, qualify

    if "#" in normalize_ref(a.ref):
        repo, num = split_ref(a.ref)
        discussion = a.discussion or "/discussions/" in a.ref
        result = qualify.run(repo, num, a.keyword or [], "discussion" if discussion else "issue")
    elif re.fullmatch(r"[\w.-]+/[\w.-]+", a.ref):
        if not a.keyword:
            raise SystemExit("a repository without an issue needs --keyword terms that a competing PR would contain")
        result = qualify.run(a.ref, None, a.keyword, "none")
    else:
        raise SystemExit(f"expected owner/repo#number, an issue URL, or owner/repo with --keyword; got {a.ref!r}")
    rules = policy.current(repo if "#" in normalize_ref(a.ref) else a.ref)
    blocked = policy.blockers(rules)
    if blocked:
        result["verdict"] = "skip"
        result.setdefault("skip", []).extend(blocked)
    result["policy"] = rules
    if a.item:
        write_json(Item(a.item).file("policy.json"), rules)
        write_json(Item(a.item).file("qualify.json"), result)
    emit(result)
    return 0 if result["verdict"] == "ok" else 2


def cmd_park(a):
    item = Item(a.item)
    reason = " ".join(a.reason.split())
    if len(reason) < 20:
        raise SystemExit("give the evidence-backed reason as a sentence: what was found and what would reopen the item")
    meta = item.meta
    if meta.get("status") in {"merged", "closed", "dropped", "abandoned"}:
        raise SystemExit(f"the item is already {meta['status']}; nothing to park")
    key = "blocked_reason" if a.blocked else "parked_reason"
    meta.update(status="blocked" if a.blocked else "parked", parked_at=iso())
    meta[key] = reason
    if a.reopen_when:
        meta["reopen_when"] = " ".join(a.reopen_when.split())
    item.save()
    emit({"item": str(item.dir), "status": meta["status"], key: reason})


def cmd_new(a):
    from factory.oss import policy, qualify

    if "#" in normalize_ref(a.ref):
        repo, num = split_ref(a.ref)
        target = "discussion" if a.discussion or "/discussions/" in a.ref else "issue"
    else:
        repo, num, target = a.ref, None, "none"
        if not a.keyword:
            raise SystemExit("an item without an issue needs --keyword terms for the collision check")
    clone = clone_dir(repo)
    if not clone.exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--filter=blob:none", f"https://github.com/{repo}.git", str(clone)])
    git(clone, "config", "user.name", CONFIG["git_name"])
    git(clone, "config", "user.email", CONFIG["git_email"])
    base = a.base or gh_json("api", f"repos/{repo}")["default_branch"]
    git(clone, "fetch", "--quiet", "origin", base)
    branch = f"contrib/{a.slug}"
    if not git(clone, "branch", "--list", branch):
        git(clone, "branch", branch, f"origin/{base}")

    day = dt.date.today().isoformat()
    item_dir = path_from_config("runs_dir") / day / "-".join(str(x) for x in (repo.split("/")[1], num, a.slug) if x is not None)
    item_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "line": "oss", "repo": repo, "issue": num, "slug": a.slug, "branch": branch, "base": base,
        "clone": str(clone), "kind": a.kind, "lane": a.lane, "implementer": a.implementer, "status": "preparing",
        "created_at": iso(), "test_cmd": None, "test_cwd": ".", "test_files": [], "keywords": [],
        "no_test_reason": None, "comment_justification": None, "allow_lockfile": False, "title_pattern": None,
        "ai_disclosure": None, "ai_disclosure_lines": [], "precondition_met": None,
        "target": target, "schema": 2, "required_checks": [], "tags": [],
    }
    meta["keywords"] = a.keyword or []
    if not (item_dir / "item.json").exists():
        write_json(item_dir / "item.json", meta)
    templates = ROOT / "lines" / "oss" / "templates"
    for t in templates.glob("*.md"):
        destination = item_dir / t.name
        if t.name == "review.md" or (t.name in ("design.md", "proposal.md") and a.kind != "large"):
            continue
        if not destination.exists():
            shutil.copy(t, destination)
    q = qualify.run(repo, num, a.keyword or [], target)
    write_json(item_dir / "qualify.json", q)
    write_json(item_dir / "policy.json", policy.current(repo))
    print(f"item: {item_dir}\nclone: {clone}\nbranch: {branch} (from origin/{base})\nqualify: {q['verdict']} {q['skip'] or ''}")
    # The clone may be shared with a worker or reviewer on another branch, so it is never switched here (2026-11-08: edits landed on the wrong branch twice).
    current = git(clone, "branch", "--show-current").strip()
    if current != branch:
        print(f"checkout: the clone is on {current or 'a detached HEAD'}; run `git -C {clone} checkout {branch}` once nothing else uses the clone, before editing")
    return 0 if q["verdict"] == "ok" else 2


def cmd_redgreen(a):
    from factory.core import redgreen

    item = Item(a.item)
    clones.ensure(item)
    result = redgreen.run(item, timeout=a.timeout)
    emit(result)
    return 0 if result["ok"] else 1


def cmd_slop(a):
    from factory.core import slop

    item = Item(a.item)
    clones.ensure(item)
    result = slop.check(item)
    emit(result)
    return 0 if result["ok"] else 1


def cmd_scan_repo(a):
    from factory.core import scan_repo

    result = scan_repo.run(a.path, a.allow_file, a.artifact)
    emit(result)
    return 0 if result["ok"] else 1


def cmd_gate(a):
    from factory.core import gate

    item = Item(a.item)
    clones.ensure(item)
    mode = getattr(a, "mode", None)
    rep, result = gate.run(item, mode=mode, with_approval=a.approval)
    print(rep.text())
    print(f"\nGATE {'PASS' if result['ok'] else 'FAIL'} at {result['head_sha'][:12]}")
    if result["ok"] and not a.approval and mode in (None, "review") and item.meta.get("status") == "preparing":
        item.meta["status"] = "ready"
        item.save()
    return 0 if result["ok"] else 1


def cmd_approve(a):
    item = Item(a.item)
    head = item.head_sha()
    standing = (CONFIG.get("standing_approval_note") or "").strip()
    by = "standing" if standing and a.note.strip().startswith(standing) else "user"
    write_json(item.file("approval.json"), {"at": iso(), "head_sha": head, "pr_sha256": _sha(item.file("pr.md")), "by": by, "note": a.note})
    item.meta["status"] = "approved"
    item.save()
    print(f"approved {item.dir.name} at {head[:12]}")


def _sha(path):
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def publication_findings(item, label, text):
    from factory.core import leaks, privacy
    from factory.core.slop import prose_findings

    fails, soft = prose_findings(label, text, allow=tuple(item.meta.get("sensitive_allow", [])))
    fails += leaks.scan(text=text, item_dir=item.dir, label=label)
    return [privacy.redact(f) for f in fails], [privacy.redact(s) for s in soft]


def cmd_strip_image(a):
    from factory.core import images

    try:
        images.strip(a.files)
    except (OSError, ValueError):
        raise SystemExit("strip-image: unreadable or invalid image; nothing published") from None
    return 0


def cmd_approve_proposal(a):
    item = Item(a.item)
    proposal = item.file(a.file)
    if not proposal.exists():
        raise SystemExit(f"{a.file} missing")
    write_json(item.file(f"approval-{proposal.stem}.json"), {"at": iso(), "sha256": _sha(proposal), "by": "user", "note": a.note})
    print(f"approved {a.file} for {item.dir.name}")


def cmd_open_issue(a):
    item = Item(a.item)
    meta, draft = item.meta, item.file(a.file)
    approval = read_json(item.file(f"approval-{draft.stem}.json"))
    if not approval or approval["sha256"] != _sha(draft):
        raise SystemExit(f"{a.file} not approved in its current form; run approve-proposal --file {a.file}")
    text = draft.read_text().strip()
    fails, soft = publication_findings(item, draft.name, text)
    if fails or len(soft) > CONFIG["limits"]["max_soft_slop_hits"]:
        raise SystemExit(f"{a.file} fails hygiene: " + "; ".join(fails + soft))
    first, _, rest = text.partition("\n")
    title, body = (first[2:].strip(), rest.strip()) if first.startswith("# ") else ((meta.get("issue_title") or "").strip(), text)
    if not title:
        raise SystemExit(f"no title: start {a.file} with '# <title>' or set issue_title in item.json")
    if meta.get("issue_title") and not first.startswith("# "):
        fails, _ = publication_findings(item, "issue_title", title)
        if fails:
            raise SystemExit("issue_title fails hygiene: " + "; ".join(fails))
    if not a.before_fix:
        clones.ensure(item)
        meta = item.meta
    if not a.before_fix and meta.get("branch"):
        ahead = git(item.clone, "rev-list", "--count", f"{item.base_ref}..{meta['branch']}", check=False)
        if not ahead.isdigit() or int(ahead) == 0:
            raise SystemExit("the fix is not built yet: an issue that names the fix invites a faster duplicate PR (L-030). "
                             "Build and gate the item first, or pass --before-fix when the repo wants discussion before code")
    if not a.before_fix:
        from factory.core import gate

        rep, _ = gate.pre_issue(item)
        if not rep.ok:
            raise SystemExit("pre-issue release checks failed; repair the evidence and re-review before opening the issue:\n" + rep.text())
    mine = gh_json("issue", "list", "-R", meta["repo"], "--author", CONFIG["github_user"], "--state", "all", "--search", title, "--json", "url,title") or []
    same = [i["url"] for i in mine if i["title"].strip() == title]
    if same:
        url = same[0]
    else:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(body + "\n")
        url = run(["gh", "issue", "create", "-R", meta["repo"], "--title", title, "--body-file", f.name]).stdout.strip().splitlines()[-1]
    meta.update(tracking_issue=url, status="awaiting-maintainer")
    if not meta.get("issue"):
        meta.update(issue=int(url.rstrip("/").rsplit("/", 1)[1]), target="issue", precondition_met=url)
    pr = item.file("pr.md")
    if pr.exists() and "#ISSUE" in pr.read_text():
        pr.write_text(pr.read_text().replace("#ISSUE", f"#{meta['issue']}"))
    meta.setdefault("issues_opened", []).append({"file": draft.name, "url": url, "at": iso()})
    item.save()
    print(url)


def cmd_propose(a):
    item = Item(a.item)
    meta, proposal = item.meta, item.file(a.file)
    approval = read_json(item.file(f"approval-{proposal.stem}.json"))
    if not approval or approval["sha256"] != _sha(proposal):
        raise SystemExit("proposal not approved in its current form; show it to the user and run approve-proposal")
    fails, soft = publication_findings(item, "proposal.md", proposal.read_text())
    if fails or len(soft) > CONFIG["limits"]["max_soft_slop_hits"]:
        raise SystemExit("proposal fails hygiene: " + "; ".join(fails + soft))
    me = CONFIG["github_user"].lower()
    body = proposal.read_text().strip()
    issue = meta.get("issue") or meta.get("tracking_issue")
    if isinstance(issue, str):
        issue = int(issue.rstrip("/").split("/")[-1].lstrip("#"))
    if not issue:
        raise SystemExit("item has neither issue nor tracking_issue; nowhere to post the proposal")
    if meta.get("target") == "discussion":
        from factory.oss import qualify

        q = qualify.run_discussion(meta["repo"], meta["issue"])
        owner, name = meta["repo"].split("/")
        query = "query($o:String!,$n:String!,$num:Int!){repository(owner:$o,name:$n){discussion(number:$num){comments(first:100){nodes{url body author{login}}}}}}"
        nodes = gh_json("api", "graphql", "-f", f"query={query}", "-F", f"o={owner}", "-F", f"n={name}", "-F", f"num={meta['issue']}")["data"]["repository"]["discussion"]["comments"]["nodes"]
        mine = [c for c in nodes if (c.get("author") or {}).get("login", "").lower() == me and c["body"].strip() == body]
        if mine:
            url = mine[0]["url"]
        else:
            mutation = "mutation($id:ID!,$body:String!){addDiscussionComment(input:{discussionId:$id,body:$body}){comment{url}}}"
            url = gh_json("api", "graphql", "-f", f"query={mutation}", "-f", f"id={q['node_id']}", "-f", f"body={body}")["data"]["addDiscussionComment"]["comment"]["url"]
    else:
        comments = gh_json("api", f"repos/{meta['repo']}/issues/{issue}/comments", "--paginate") or []
        mine = [c for c in comments if c["user"]["login"].lower() == me and c["body"].strip() == body]
        if mine:
            url = mine[0]["html_url"]
        else:
            url = run(["gh", "issue", "comment", str(issue), "-R", meta["repo"], "--body-file", str(proposal)]).stdout.strip()
    if proposal.name == "proposal.md":
        meta.update(proposal_url=url, status="awaiting-maintainer")
    else:
        meta.setdefault("issue_comments", []).append({"file": proposal.name, "url": url, "at": iso()})
    item.save()
    print(url)


def cmd_approve_reply(a):
    item = Item(a.item)
    reply = item.file(a.file)
    if not reply.exists():
        raise SystemExit(f"{reply} missing")
    write_json(item.file(f"approval-{reply.stem}.json"), {"at": iso(), "sha256": _sha(reply), "by": "user", "note": a.note})
    print(f"approved {reply.name} for {item.dir.name}")


def cmd_reply(a):
    """Post an approved reply on the item's PR. Idempotent: an identical comment is recorded, not reposted."""
    item = Item(a.item)
    meta, reply = item.meta, item.file(a.file)
    approval = read_json(item.file(f"approval-{reply.stem}.json"))
    if not approval or approval["sha256"] != _sha(reply):
        raise SystemExit(f"{reply.name} not approved in its current form; show it to the user and run approve-reply")
    if not meta.get("pr_url"):
        raise SystemExit("item has no pr_url; replies go on an open PR")
    withdrawn = [r for r in meta.get("replies", []) if r.get("withdrawn_at")
                 and (r.get("file") == reply.name or r.get("sha256") == _sha(reply))]
    if withdrawn:
        # A comment the owner had deleted stays deleted: no repost, no substitute (vercel/ai #24322, 2026-11-25).
        raise SystemExit(f"{reply.name} was withdrawn on {withdrawn[0]['withdrawn_at']} ({withdrawn[0].get('withdrawn_reason', 'owner')}); "
                         "do not repost it or post a replacement")
    fails, soft = publication_findings(item, reply.name, reply.read_text())
    if fails or len(soft) > CONFIG["limits"]["max_soft_slop_hits"]:
        raise SystemExit(f"{reply.name} fails hygiene: " + "; ".join(fails + soft))
    body = reply.read_text().strip()
    me = CONFIG["github_user"].lower()
    pr = gh_json("pr", "view", meta["pr_url"], "--json", "comments,state,headRefOid", timeout=GH_READ_TIMEOUT)
    changes = []
    if pr["state"] != "OPEN":
        changes.append(f"PR is {pr['state']} (expected OPEN)")
    expected = meta.get("pushed_head") or meta.get("head_sha")
    if not expected:
        changes.append("item has no recorded pushed_head or head_sha")
    elif pr["headRefOid"] != expected:
        changes.append(f"head changed from {expected} to {pr['headRefOid']}; someone else may have pushed")
    if changes:
        message = "; ".join(changes)
        if not a.force:
            raise SystemExit(f"reply refused: {message}. Re-read the PR before using --force")
        print(f"warning: --force overrides reply check: {message}", file=sys.stderr)
    mine = [c for c in pr["comments"] if (c.get("author") or {}).get("login", "").lower() == me and c["body"] == body]
    url = mine[0]["url"] if mine else run(["gh", "pr", "comment", meta["pr_url"], "--body", body]).stdout.strip()
    meta.setdefault("replies", []).append({"file": reply.name, "url": url, "at": iso()})
    item.save()
    print(url)


def cmd_submit(a):
    from factory.core import clean
    from factory.oss import submit

    item = Item(a.item)
    clones.ensure(item)
    result = submit.run(item, dry_run=a.dry_run, draft=a.draft)
    if "report" in result:
        print(result.pop("report"))
    if result["ok"] and not a.dry_run:
        result["deps_removed"] = clean.after_submit(item)
    emit(result)
    return 0 if result["ok"] else 1


def cmd_followup(a):
    from factory.core import notify
    from factory.oss import followup

    actions = followup.run() + followup.waiting_issues()
    for line in actions:
        print(line)
    if not actions:
        print("nothing needs attention")
    elif a.notify:
        notify.send(f"{len(actions)} PR updates need you: " + actions[0][:150])


def cmd_clean(a):
    from factory.core import clean

    emit(clean.run_clean(prune=a.prune, dry_run=a.dry_run))


def cmd_ledger(a):
    from factory.core import ledger

    rows = ledger.load()
    ledger.render(rows)
    print((ROOT / "ledger" / "README.md").read_text())


def cmd_recorded(a):
    from factory.core import ledger
    from factory.util import iso

    with ledger.locked():
        rows = ledger.load()
        missing = [url for url in a.urls if not ledger.find(rows, url)]
        if missing:
            raise SystemExit(f"not in the ledger: {', '.join(missing)}")
        for url in a.urls:
            row = ledger.find(rows, url)
            if row["status"] != "merged":
                raise SystemExit(f"{url} is {row['status']}, not merged")
            row["recorded_at"] = iso()
        ledger.save(rows)
    print(f"recorded {len(a.urls)}")


def cmd_lesson(a):
    from factory.core import ledger, lessons

    add = getattr(lessons, "add_legacy", ledger.add_lesson)
    lid = add(a.title, a.rule, a.scope, a.source or a.pr or "", a.enforced_by, a.status, a.pr)
    print(lid)


def cmd_status(a):
    from factory.core import ledger

    rows = ledger.load()
    by = {}
    for r in rows:
        by.setdefault(r["status"], []).append(r["url"])
    items = {}
    for meta_path in sorted(path_from_config("runs_dir").glob("*/*/item.json")):
        meta = read_json(meta_path)
        items.setdefault(meta.get("status", "?"), []).append(str(meta_path.parent.relative_to(ROOT)))
    emit({"prs": by, "items": items, "missing_lessons": [r["url"] for r in ledger.missing_lessons(rows)]})


def cmd_notify(a):
    from factory.core import notify

    notify.send(a.message)


def cmd_stop_check(a):
    from factory.core import stopcheck

    return stopcheck.main()


# loop-v2: plan-influence
def cmd_plan(a):
    from factory.oss import plan

    report = plan.run()
    emit(report)
    return 1 if report["errors"] else 0


def cmd_influence(a):
    from factory.oss import plan

    report = plan.influence(record=a.record)
    emit(report)
    failed = report.get("profile_error") or report.get("record_refused") or any("error" in r for r in report["repos"])
    return 1 if failed else 0
# end loop-v2: plan-influence


# loop-v2: profile-check
def cmd_profile_check(a):
    from factory.oss import profile_readme

    try:
        result = profile_readme.run(a.readme)
    except ValueError as error:
        raise SystemExit(f"profile-check refused: {error}") from None
    emit(result)
    return 0 if result["ok"] else 1
# end loop-v2: profile-check


# loop-v2: commit-lock
def cmd_commit(a):
    from factory.core import commit

    result = commit.run_commit(a.message, a.path or [], push=not a.no_push)
    emit(result)
    return 0 if result["ok"] else 1


def cmd_lock(a):
    from factory.core import shiftlock

    generation = getattr(a, "generation", None)
    options = {"generation": generation} if generation is not None or a.action == "renew" else {}
    result = shiftlock.run(a.action, a.owner, a.ttl_hours, a.force, **options)
    emit(result)
    return 0 if result["ok"] else 1
# end loop-v2: commit-lock


# loop-v2: ship
def cmd_ship(a):
    from factory.oss import ship

    result = ship.run(a.item, dry_run=a.dry_run)
    emit(result)
    return 0 if result["ok"] and not result["warnings"] else 1
# end loop-v2: ship


# loop-v2: followup-threads
def cmd_thread_reply(a):
    """Post an approved reply inside an inline review thread. Idempotent within that thread."""
    from factory.oss import thread_reply

    item = Item(a.item)
    name, text = thread_reply.approved_text(item, a.file)
    fails, soft = publication_findings(item, name, text)
    if fails or len(soft) > CONFIG["limits"]["max_soft_slop_hits"]:
        raise SystemExit(f"{name} fails hygiene: " + "; ".join(fails + soft))
    print(thread_reply.run(item, name, text.strip(), a.comment_id, force=a.force))
# end loop-v2: followup-threads


# loop-v2: clone-heal
def cmd_clone_heal(a):
    from factory.core import clone_heal

    result = clone_heal.heal(a.clone, dry_run=a.dry_run)
    emit(result)
    return 0 if result["ok"] else 1
# end loop-v2: clone-heal


# loop-v3: repo-rename
def cmd_repo_renames(a):
    from factory.core import repos

    if not a.check:
        repos.renames()
        emit({"repo_renames": CONFIG.get("repo_renames", {})})
        return 0
    result = repos.check()
    emit(result)
    return 0 if result["ok"] else 1
# end loop-v3: repo-rename


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] in (["gh"], ["push"]):
        from factory.core import publication

        return publication.main(argv[0], argv[1:])
    p = argparse.ArgumentParser(prog="factory", description="Open-source contribution factory")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("gh", help="scan exact gh arguments before publication; [-C repo] [--item item] [--] <gh args>")
    sub.add_parser("push", help="scan exact git push updates; [-C repo] [--item item] [--] <push args>")
    sub.add_parser("doctor", help="check the environment").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("strip-image", help="remove image metadata without re-encoding pixels")
    s.add_argument("files", nargs="+")
    s.set_defaults(fn=cmd_strip_image)

    s = sub.add_parser("health", help="repo merge stats and AI-flood index")
    s.add_argument("repos", nargs="+")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--responsiveness", action="store_true",
                   help="the first-PR test: did maintainers answer outside PRs within unresponsive_days, over a long record")
    s.add_argument("--refresh", action="store_true", help="with --responsiveness: read GitHub again instead of the saved record")
    s.set_defaults(fn=cmd_health)

    s = sub.add_parser("policy", help="contribution and AI rules of a repo")
    s.add_argument("repo")
    s.add_argument("--endorse", action="store_true",
                   help="coordinator only: record that the owner's standing endorsement covers the repo's current AI-policy rules, "
                        "which may only ask a person to take responsibility for or review AI work")
    s.add_argument("--note", help="with --endorse: which rules ask for that and why nothing else is restricted")
    s.set_defaults(fn=cmd_policy)

    s = sub.add_parser("qualify", help="is owner/repo#N still free and worth it")
    s.add_argument("ref")
    s.add_argument("--keyword", action="append")
    s.add_argument("--discussion", action="store_true", help="N is a GitHub Discussion, not an issue")
    s.add_argument("--item")
    s.set_defaults(fn=cmd_qualify)

    s = sub.add_parser("new", help="start an item: clone, branch, item dir, qualify, policy")
    s.add_argument("ref")
    s.add_argument("--slug", required=True)
    s.add_argument("--kind", choices=["small", "large"], default="small")
    s.add_argument("--lane", default="")
    s.add_argument("--base")
    s.add_argument("--implementer", default="", help="model/agent that writes the code")
    s.add_argument("--discussion", action="store_true", help="N is a GitHub Discussion, not an issue")
    s.add_argument("--keyword", action="append", help="terms a competing PR would contain (required without an issue)")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("redgreen", help="prove the test fails without the fix and passes with it")
    s.add_argument("item")
    s.add_argument("--timeout", type=int, default=1800)
    s.set_defaults(fn=cmd_redgreen)

    s = sub.add_parser("slop", help="diff and prose hygiene")
    s.add_argument("item")
    s.set_defaults(fn=cmd_slop)

    s = sub.add_parser(
        "scan-repo", help="scan HEAD, all-ref history and untracked non-ignored files before publishing",
        epilog="Placeholder hosts include reserved domains, uppercase labels and labels containing your/example. "
        "Binary files require review; shallow history cannot pass. "
        "Local config private_terms and private_repos are never printed.",
    )
    s.add_argument("path")
    s.add_argument("--allow-file", help='JSON array of {"string": "exact match", "reason": "why it is fake"}')
    s.add_argument("--artifact", action="append", metavar="file", help="also scan a .tar.gz/.tgz/.tar/.zip/.whl archive without extracting (repeatable)")
    s.set_defaults(fn=cmd_scan_repo)

    s = sub.add_parser("gate", help="all checks an item must pass before a human sees it")
    s.add_argument("item")
    s.add_argument("--approval", action="store_true", help="use release mode, including coordinator review and human approval")
    s.add_argument("--mode", choices=["prepare", "review", "release"], help="check preparation, independent review (default), or release")
    s.set_defaults(fn=cmd_gate)

    s = sub.add_parser("approve", help="record the user's approval for the current head (only after they said yes)")
    s.add_argument("item")
    s.add_argument("--note", required=True, help="the user's words")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("approve-proposal", help="record the user's approval of proposal.md (or --file) as it is now")
    s.add_argument("item")
    s.add_argument("--file", default="proposal.md")
    s.add_argument("--note", required=True)
    s.set_defaults(fn=cmd_approve_proposal)

    s = sub.add_parser("propose", help="post the approved proposal.md (or --file) as an issue or discussion comment (idempotent)")
    s.add_argument("item")
    s.add_argument("--file", default="proposal.md")
    s.set_defaults(fn=cmd_propose)

    s = sub.add_parser("open-issue", help="open the approved issue.md (or --file) as a new issue in the item's repo (idempotent)")
    s.add_argument("item")
    s.add_argument("--file", default="issue.md")
    s.add_argument("--before-fix", action="store_true", help="discussion before code: only the current issue text approval is required, not code release checks")
    s.set_defaults(fn=cmd_open_issue)

    s = sub.add_parser("approve-reply", help="record the user's approval of a reply file as it is now")
    s.add_argument("item")
    s.add_argument("--file", default="reply.md")
    s.add_argument("--note", required=True)
    s.set_defaults(fn=cmd_approve_reply)

    s = sub.add_parser("reply", help="post an approved reply on the item's PR (idempotent)")
    s.add_argument("item")
    s.add_argument("--file", default="reply.md")
    s.add_argument("--force", action="store_true", help="override closed/merged PR or changed/missing recorded head checks")
    s.set_defaults(fn=cmd_reply)

    s = sub.add_parser("submit", help="fork, push, open the PR")
    s.add_argument("item")
    s.add_argument("--dry-run", action="store_true", help="no push or PR/issue writes; read-only GitHub calls and fetch may run and refresh item caches (gate.json, slop.json, policy.json, qualify.json); may restore a released clone and remove its release record from item.json; no other item or ledger changes")
    s.add_argument("--draft", action="store_true")
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("retro", help="phase retrospective numbers for our PRs since a date")
    s.add_argument("--since", required=True, help="YYYY-MM-DD, first creation date counted")
    s.set_defaults(fn=cmd_retro)

    from factory.oss import watch

    s = sub.add_parser("watch", help="read-only delta scan for opportunities in watched repos", description=watch.HELP)
    s.add_argument("--repo", action="append", help="limit to this watched repo (repeatable)")
    s.add_argument("--since", help="override the cursor with an ISO timestamp including timezone")
    s.add_argument("--dry-run", action="store_true", help="write the report without advancing cursors")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("followup", help="CI, reviews and comments on open PRs")
    s.add_argument("--notify", action="store_true")
    s.set_defaults(fn=cmd_followup)

    s = sub.add_parser("clean", help="drop dependency and build output; idle clones use clone release --idle",
                       description="Drop dependency and build output; idle clones are handled by bin/factory clone release --idle.")
    s.add_argument("--prune", action="store_true", help="remove worktrees and clones with no live item and nothing unpushed")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_clean)

    sub.add_parser("ledger", help="render and print the PR ledger").set_defaults(fn=cmd_ledger)

    s = sub.add_parser("recorded", help="mark merged PRs as handled (Private notebook, Teach, queue) so follow-up stops reporting them")
    s.add_argument("urls", nargs="+")
    s.set_defaults(fn=cmd_recorded)

    s = sub.add_parser("lesson", help="record a lesson")
    s.add_argument("action", choices=["add"])
    s.add_argument("--title", required=True)
    s.add_argument("--rule", required=True)
    s.add_argument("--scope", default="global")
    s.add_argument("--source")
    s.add_argument("--enforced-by", default="manual")
    s.add_argument("--status", default="active", choices=["candidate", "active", "disputed", "retired"])
    s.add_argument("--pr")
    s.set_defaults(fn=cmd_lesson)

    sub.add_parser("status", help="items and PRs by status").set_defaults(fn=cmd_status)

    s = sub.add_parser("notify", help="desktop notification")
    s.add_argument("message")
    s.set_defaults(fn=cmd_notify)

    sub.add_parser("stop-check", help="Claude Code Stop hook").set_defaults(fn=cmd_stop_check)

    # loop-v2: plan-influence
    sub.add_parser("plan", help="read-only shift plan: items ready to gate, fix rounds, stale reviews, repo slots").set_defaults(fn=cmd_plan)
    s = sub.add_parser("influence", help="stars, merged count and contributor rank in each repo that merged our work, plus followers")
    s.add_argument("--record", action="store_true", help="append this reading to ledger/influence.jsonl")
    s.set_defaults(fn=cmd_influence)
    # end loop-v2: plan-influence
    # loop-v2: profile-check
    s = sub.add_parser("profile-check", help="read-only: is the GitHub profile README behind the ledger's merges, and is it free of private data")
    s.add_argument("--readme", help="check this local file instead of fetching <github_user>/<github_user> README.md")
    s.set_defaults(fn=cmd_profile_check)
    # end loop-v2: profile-check
    # loop-v2: commit-lock
    s = sub.add_parser("commit", help="commit selected factory paths (all tracked changes if omitted), check size and gitleaks, push")
    s.add_argument("-m", "--message", required=True)
    s.add_argument("--path", action="append", help="stage only these paths inside the factory; refuse unrelated staged changes (repeatable)")
    s.add_argument("--no-push", action="store_true")
    s.set_defaults(fn=cmd_commit)

    s = sub.add_parser("lock", help="shift lock so two sessions never run a shift at the same time")
    s.add_argument("action", choices=["acquire", "renew", "release", "status"])
    s.add_argument("--owner")
    s.add_argument("--generation", help="lock generation returned by acquire")
    s.add_argument("--ttl-hours", type=float, default=4)
    s.add_argument("--force", action="store_true", help="release a lock someone else holds")
    s.set_defaults(fn=cmd_lock)
    # end loop-v2: commit-lock
    # loop-v2: ship
    s = sub.add_parser("ship", help="send one reviewed item: automation disclosure, issue first, gate, standing approval, submit, verify, clean")
    s.add_argument("item", help="path to an item directory under runs/ (from the cwd or the factory root), or the exact name of one")
    s.add_argument("--dry-run", action="store_true", help="no push, PR/issue writes or approvals; read-only GitHub calls and fetch may run and refresh item caches (gate.json, slop.json, policy.json, qualify.json); may restore a released clone and remove its release record from item.json; no other item or ledger changes")
    s.set_defaults(fn=cmd_ship)
    # end loop-v2: ship
    # loop-v2: followup-threads
    s = sub.add_parser("thread-reply", help="post an approved reply inside an inline review thread of the item's PR (idempotent)")
    s.add_argument("item")
    s.add_argument("--comment-id", type=int, required=True, help="id of the thread's first comment (BOT FINDING lines print it)")
    s.add_argument("--file", required=True)
    s.add_argument("--force", action="store_true", help="override closed/merged PR or changed/missing recorded head checks")
    s.set_defaults(fn=cmd_thread_reply)
    # end loop-v2: followup-threads
    # loop-v2: clone-heal
    s = sub.add_parser("clone-heal", help="rebuild a clone missing objects as a blobless clone and carry its contrib/* branches over (L-064)")
    s.add_argument("clone")
    s.add_argument("--dry-run", action="store_true", help="run the checks and report what would happen")
    s.set_defaults(fn=cmd_clone_heal)
    # end loop-v2: clone-heal
    # loop-v3: repo-rename
    s = sub.add_parser("repo-renames", help="print the repo_renames map (old -> current owner/name); --check compares it with GitHub (read-only)")
    s.add_argument("--check", action="store_true", help="ask GitHub for the current name of every ledger repo and report any rename the map lacks; never edits the config")
    s.set_defaults(fn=cmd_repo_renames)
    # end loop-v3: repo-rename

    from factory import extensions

    extensions.register(sub)

    s = sub.add_parser("park", help="park or block an item with its evidence-backed reason")
    s.add_argument("item")
    s.add_argument("--reason", required=True, help="what was found and what would reopen the item")
    s.add_argument("--blocked", action="store_true",
                   help="record status blocked: the item waits on something outside the factory")
    s.add_argument("--reopen-when")
    s.set_defaults(fn=cmd_park)

    clones.register(sub)
    from factory.core import metrics, ownerops, public

    ownerops.register(sub)
    metrics.register(sub)
    public.register(sub)

    a = p.parse_args(argv)
    try:
        return a.fn(a) or 0
    except CommandError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
