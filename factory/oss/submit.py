"""Open the PR. The only command in the factory that writes to someone else's repository.

Refuses unless the gate passes with a human approval bound to the current HEAD, the flood
limits leave room, every earlier closed PR has a lesson, and no PR exists for this branch yet
(so a retry after a timeout never opens a duplicate).
"""
import copy
import datetime as dt
import hashlib
import pathlib
import tempfile
import time

from factory.core import clone_heal, gate, ledger, privacy_git, publication, published
from factory.core.repos import canonical
from factory.core.slop import parse_pr_md
from factory.oss import disclosure, qualify
from factory.util import CONFIG, CommandError, gh_json, git, iso, now, parse_iso, read_json, run as sh, write_json


def open_prs(me):
    """Our open PRs in other people's repos. PRs opened long before the factory are history, not a current spread."""
    data = gh_json("search", "prs", "--author", me, "--state", "open", "--json", "repository,url,createdAt", "--limit", "100") or []
    since = now() - dt.timedelta(days=CONFIG["limits"].get("open_pr_window_days", 180))
    return [
        p for p in data
        if not p["repository"]["nameWithOwner"].lower().startswith(me.lower() + "/") and parse_iso(p["createdAt"]) >= since
    ]


def merged_repos(me):
    """Repo -> when our latest PR there merged (None when GitHub gives no date)."""
    data = gh_json("search", "prs", "--author", me, "--merged", "--json", "repository,closedAt", "--limit", "200") or []
    last = {}
    for p in data:
        repo = canonical(p["repository"]["nameWithOwner"]).lower()
        at = parse_iso(p["closedAt"]) if p.get("closedAt") else None
        if repo not in last or (at and (last[repo] is None or at > last[repo])):
            last[repo] = at
    return last


def flood_check(repo, me):
    """Repos that merged our work are bounded by their own cap; the total cap bounds exploration elsewhere.
    A repo that leaves our PRs unmerged for days gets no new ones until it merges again."""
    limits = CONFIG["limits"]
    problems = []
    mine = open_prs(me)
    proven = merged_repos(me)
    key = canonical(repo).lower()
    in_repo = [p for p in mine if canonical(p["repository"]["nameWithOwner"]).lower() == key]
    merged_here = key in proven
    cap = limits["max_open_prs_per_repo"] if merged_here else limits["max_open_prs_per_repo_before_first_merge"]
    if len(in_repo) >= cap:
        problems.append(f"{len(in_repo)} open PRs in {repo} (cap {cap}{'' if merged_here else ' until the first merge'})")
    stale_days = limits.get("stale_open_days", 3)
    cutoff = now() - dt.timedelta(days=stale_days)
    last_merge = proven.get(key)
    stale = [
        p for p in in_repo
        if parse_iso(p["createdAt"]) <= cutoff and (last_merge is None or last_merge < parse_iso(p["createdAt"]))
    ]
    if stale:
        problems.append(f"{len(stale)} open PRs in {repo} unmerged for {stale_days}+ days with no merge since (paused until one merges)")
    exploring = [p for p in mine if canonical(p["repository"]["nameWithOwner"]).lower() not in proven]
    if not merged_here and len(exploring) >= limits["max_open_prs_total"]:
        problems.append(f"{len(exploring)} open PRs in repos without a merge yet (cap {limits['max_open_prs_total']})")
    daily_cap = limits.get("max_new_prs_per_day")
    if daily_cap is not None:
        from zoneinfo import ZoneInfo
        from factory.core.metrics import ACTIVITY_ZONE

        zone = ZoneInfo(ACTIVITY_ZONE)
        today = now().astimezone(zone).date()
        sent_today = [r for r in ledger.load() if r.get("submitted_at") and parse_iso(r["submitted_at"]).astimezone(zone).date() == today]
        if len(sent_today) >= daily_cap:
            problems.append(f"{len(sent_today)} PRs opened today (cap {daily_cap})")
    return problems


def preconditions(item):
    """The read-only refusals that need neither the gate nor an issue number; the reason, or None when they pass."""
    missing = ledger.missing_lessons()
    if missing:
        return "closed or silent PRs without a lesson: " + ", ".join(r["url"] for r in missing)
    problems = flood_check(item.meta["repo"], CONFIG["github_user"])
    if problems:
        return "flood limits: " + "; ".join(problems)
    stacked_on = item.meta.get("stacked_on")
    if stacked_on and stacked_on not in parse_pr_md(item.file("pr.md").read_text())[1]:
        return f"stacked item: pr.md must say what it builds on ({stacked_on} or its PR link)"
    return None


def ensure_fork(repo, me):
    name = repo.split("/")[1]
    try:
        data = gh_json("api", f"repos/{me}/{name}")
        if (data.get("parent") or {}).get("full_name", "").lower() == repo.lower():
            return data["full_name"]
    except CommandError:
        pass
    sh(["gh", "repo", "fork", repo, "--clone=false", "--default-branch-only"])
    for _ in range(30):
        try:
            data = gh_json("api", f"repos/{me}/{name}")
            return data["full_name"]
        except CommandError:
            time.sleep(2)
    raise SystemExit(f"fork of {repo} did not appear under {me}")


def target_privacy(item, public=None, head=None):
    try:
        repo = publication.repository_name(item.meta["repo"])
        base = item.meta.get("base", "main")
        git(item.clone, "check-ref-format", "refs/heads/" + base)
        git(item.clone, "fetch", "--quiet", "--no-tags", "--", f"https://github.com/{repo}.git", "refs/heads/" + base)
        target = git(item.clone, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        if public is not None:
            public.append(target)
        head = head or item.head_sha()
        merge_base = git(item.clone, "merge-base", target, head)
        _, _, ctx = publication.context(item.dir)
        return privacy_git.scan(item.clone, [head, "^" + merge_base], names=[repo, base, item.meta["branch"]],
                                on_pr_branch=published.on_pr_branch(item, merge_base), **ctx)
    except CommandError as error:
        return [clone_heal.refresh_hint(item.clone, error) or "target branch refresh or privacy scan failed; no push attempted"]
    except (OSError, ValueError):
        return ["target branch refresh or privacy scan failed; no push attempted"]


def privacy_refusal(found):
    """A damaged clone names its repair instead of the generic scan failure (L-064)."""
    heal = next((f for f in found if clone_heal.COMMAND in f), None)
    return {"ok": False, "reason": heal or "target privacy scan failed", "findings": found}


def push_privacy(item, args, public=()):
    try:
        return publication.check_push(args, item.clone, item.dir, public)
    except (OSError, ValueError):
        return ["push preflight failed; no push attempted"]


def recover_existing(item, pr):
    """Insert a missing PR row without overwriting follow-up or owner fields."""
    with ledger.locked():
        rows = ledger.load()
        if ledger.find(rows, pr["url"]) is not None:
            return
        meta = item.meta
        status = {"MERGED": "merged", "CLOSED": "closed"}.get(pr["state"], "submitted")
        row = {
            "url": pr["url"], "repo": meta["repo"], "number": int(pr["url"].rstrip("/").split("/")[-1]),
            "issue": meta.get("issue"), "branch": meta["branch"], "item": str(item.dir),
            "kind": meta.get("kind", "small"), "status": status,
            "submitted_at": pr.get("createdAt") or meta.get("submitted_at") or iso(),
            "last_check": iso(), "lesson": None, "notes": meta.get("title", ""),
        }
        if pr.get("mergedAt"):
            row["merged_at"] = pr["mergedAt"]
        rows.append(row)
        ledger.save(rows)


def inputs_changed(item, head, text):
    try:
        approval = read_json(item.file("approval.json")) or {}
        unchanged = (item.head_sha() == head and item.file("pr.md").read_bytes() == text
                     and approval.get("head_sha") == head
                     and approval.get("pr_sha256") == hashlib.sha256(text).hexdigest())
    except (OSError, ValueError, CommandError):
        unchanged = False
    if not unchanged:
        return "the branch, pr.md or its approval changed during release; re-review the current head and PR text, then approve again"
    return None


def remote_changed(item, head):
    remote = git(item.clone, "ls-remote", "fork", f"refs/heads/{item.meta['branch']}").split()
    if not remote or remote[0] != head:
        return "the remote branch does not match the approved head; fetch it and re-review before releasing again"
    return None


def update_existing(item, pr, dry_run=False):
    """Push follow-up commits only after the release gate; dry runs only refresh cached check files."""
    if dry_run:
        item = copy.copy(item)
        item.meta = dict(item.meta)
    meta, branch = item.meta, item.meta["branch"]
    meta["pr_url"] = pr["url"]
    if not dry_run:
        item.save()
        recover_existing(item, pr)
    if pr["state"] != "OPEN":
        return {"ok": True, "url": pr["url"], "note": f"PR is {pr['state'].lower()}; nothing pushed"}
    if "fork" not in git(item.clone, "remote").split():
        return {"ok": False, "reason": "PR exists but the clone has no 'fork' remote to update"}
    remote = git(item.clone, "ls-remote", "fork", f"refs/heads/{branch}").split()
    remote_head = remote[0] if remote else None
    head = item.head_sha()
    if remote_head == head:
        return {"ok": True, "url": pr["url"], "note": "PR already up to date"}
    text = item.file("pr.md").read_bytes()
    rep, result = gate.run(item, with_approval=True)
    if not rep.ok:
        return {"ok": False, "reason": "gate failed for the update", "report": rep.text()}
    if result["head_sha"] != head:
        return {"ok": False, "reason": "the branch moved during the gate; re-review the current head and PR text"}
    reason = inputs_changed(item, head, text)
    if reason:
        return {"ok": False, "reason": reason}
    fast_forward = remote_head and sh(["git", "-C", str(item.clone), "merge-base", "--is-ancestor", remote_head, head], check=False).returncode == 0
    if remote_head and not fast_forward and remote_head != meta.get("pushed_head"):
        return {"ok": False, "reason": f"the PR branch moved since our last push ({remote_head[:9]}); someone else pushed to it. Fetch and build on their commits before updating"}
    if dry_run:
        return {"ok": True, "dry_run": True, "url": pr["url"], "from": remote_head, "to": head,
                "forced": bool(remote_head and not fast_forward), "note": "would push the approved update"}
    public = []
    found = target_privacy(item, public, head=head)
    if found:
        return privacy_refusal(found)
    args = ["fork", f"{head}:refs/heads/{branch}"]
    if remote_head and not fast_forward:
        git(item.clone, "fetch", "-q", "fork", branch)
        args.insert(0, f"--force-with-lease={branch}:{remote_head}")
    found = push_privacy(item, args, public)
    if found:
        return {"ok": False, "reason": "push privacy scan failed", "findings": found}
    reason = inputs_changed(item, head, text)
    if reason:
        return {"ok": False, "reason": reason}
    git(item.clone, "push", *args)
    reason = remote_changed(item, head) or inputs_changed(item, head, text)
    if reason:
        return {"ok": False, "reason": reason, "url": pr["url"]}
    meta.update(status="submitted", updated_at=iso(), pushed_head=head)
    item.save()
    return {"ok": True, "url": pr["url"], "updated": True, "from": remote_head, "to": head, "forced": bool(remote_head and not fast_forward)}


def run(item, dry_run=False, draft=False):
    meta = item.meta
    me = CONFIG["github_user"]
    repo, branch = meta["repo"], meta["branch"]

    existing = gh_json("pr", "list", "-R", repo, "--head", branch, "--author", me, "--state", "all", "--json", "url,state,createdAt,mergedAt", "--limit", "5") or []
    if existing:
        result = update_existing(item, existing[0], dry_run=dry_run)
        if dry_run:
            result["dry_run"] = True
        return result

    checkpoint = item.file("submit-qualify.json")
    qualified_at = (read_json(checkpoint) or {}).get("since") or (read_json(item.file("qualify.json")) or {}).get("at")
    head, text = item.head_sha(), item.file("pr.md").read_bytes()
    rep, result = gate.run(item, with_approval=True)
    if not rep.ok:
        return {"ok": False, "reason": "gate failed", "report": rep.text()}
    if result["head_sha"] != head:
        return {"ok": False, "reason": "the branch moved during the gate; re-review the current head and PR text"}
    reason = inputs_changed(item, head, text) or preconditions(item)
    if not reason:
        reason = inputs_changed(item, head, text)
    if reason:
        return {"ok": False, "reason": reason}

    title, body = parse_pr_md(text.decode("utf-8"))
    try:
        body = disclosure.new_pr_body(body, read_json(item.file("policy.json")) or {})
    except ValueError as error:
        return {"ok": False, "reason": str(error)}
    _, _, context = publication.context(item.dir)
    found = publication.text_findings("PR body", body, context)
    if found:
        return {"ok": False, "reason": "PR body scan failed", "findings": found}
    if meta.get("stacked_on"):
        draft = True
    if dry_run:
        return {"ok": True, "dry_run": True, "title": title, "body": body, "base": meta.get("base", "main"), "head": f"{me}:{branch}"}

    public = []
    found = target_privacy(item, public, head=head)
    if found:
        return privacy_refusal(found)
    reason = inputs_changed(item, head, text)
    if reason:
        return {"ok": False, "reason": reason}
    fork = ensure_fork(repo, me)
    remotes = git(item.clone, "remote").split()
    if "fork" not in remotes:
        git(item.clone, "remote", "add", "fork", f"https://github.com/{fork}.git")
    args = ["fork", f"{head}:refs/heads/{branch}"]
    found = push_privacy(item, args, public)
    if found:
        return {"ok": False, "reason": "push privacy scan failed", "findings": found}
    reason = inputs_changed(item, head, text)
    if reason:
        return {"ok": False, "reason": reason}
    git(item.clone, "push", *args)
    reason = remote_changed(item, head) or inputs_changed(item, head, text)
    if reason:
        return {"ok": False, "reason": reason}

    qualified_at = qualified_at or (read_json(item.file("qualify.json")) or {}).get("at")
    if not qualified_at:
        return {"ok": False, "reason": "no qualification timestamp; run bin/factory qualify before submitting"}
    write_json(checkpoint, {"since": qualified_at, "at": iso(), "verdict": "checking"})
    try:
        fresh = qualify.fresh_collisions(item, qualified_at)
    except (CommandError, ValueError, KeyError, TypeError) as error:
        write_json(checkpoint, {"since": qualified_at, "at": iso(), "verdict": "error", "skip": [str(error)]})
        return {"ok": False, "reason": f"fresh collision check failed; no PR created: {error}"}
    fresh["since"] = qualified_at
    write_json(checkpoint, fresh)
    if fresh["verdict"] != "ok":
        return {"ok": False, "reason": "fresh collision check blocked submission: " + "; ".join(fresh.get("skip", [])),
                "collisions": fresh.get("collisions", [])}

    reason = inputs_changed(item, head, text) or remote_changed(item, head)
    if reason:
        return {"ok": False, "reason": reason}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
        f.write(body)
        body_file = f.name
    args = ["gh", "pr", "create", "-R", repo, "--base", meta.get("base", "main"), "--head", f"{fork.split('/')[0]}:{branch}",
            "--title", title, "--body-file", body_file]
    if draft:
        args.append("--draft")
    try:
        url = sh(args).stdout.strip().splitlines()[-1]
    finally:
        pathlib.Path(body_file).unlink(missing_ok=True)

    number = int(url.rstrip("/").split("/")[-1])
    meta.update(pr_url=url, status="submitted", submitted_at=iso(), pushed_head=head)
    item.save()
    ledger.upsert({
        "url": url, "repo": repo, "number": number, "issue": meta.get("issue"), "branch": branch,
        "item": str(item.dir), "kind": meta.get("kind", "small"), "status": "submitted",
        "submitted_at": meta["submitted_at"], "last_check": iso(), "lesson": None, "notes": meta.get("title", ""),
    })
    reason = remote_changed(item, head) or inputs_changed(item, head, text)
    if reason:
        return {"ok": False, "url": url, "reason": reason}
    return {"ok": True, "url": url}
