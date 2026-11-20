"""Account operations the owner delegated: close our own pull request, describe our own repository, delete an idle fork.

The publication wrapper refuses these as generic gh writes. Each command here checks what makes the action safe,
performs exactly one write and appends it to ledger/owner-ops.jsonl. --note quotes the instruction that delegates it.
"""
import json
import re
from urllib.parse import quote

from factory.core import leaks, privacy
from factory.core.slop import prose_findings
from factory.util import CONFIG, ROOT, CommandError, GH_READ_TIMEOUT, gh_json, iso, run

LOG = ROOT / "ledger" / "owner-ops.jsonl"
PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9]\d*)/?")
TOPIC = re.compile(r"[a-z0-9][a-z0-9-]{0,49}")
FACTORY_BRANCHES = "contrib/"
PAGE = 100


def _me():
    return CONFIG["github_user"].lower()


def _record(action, target, note, **facts):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": iso(), "action": action, "target": target, "note": note, **facts},
                                ensure_ascii=False) + "\n")


def close_pr(url, note):
    match = PR_URL.fullmatch(url.strip())
    if not match:
        raise CommandError("pass the pull request URL: https://github.com/<owner>/<repo>/pull/<number>")
    pr = gh_json("pr", "view", url, "--json", "author,state,title", timeout=GH_READ_TIMEOUT) or {}
    if ((pr.get("author") or {}).get("login") or "").lower() != _me():
        raise CommandError("only our own pull request can be closed here; answer another author's with bin/factory reply")
    if pr.get("state") != "OPEN":
        raise CommandError(f"the pull request is {str(pr.get('state')).lower()}, not open; nothing to close")
    run(["gh", "pr", "close", match[2], "--repo", match[1]])
    _record("close-pr", url, note, title=pr.get("title"))
    return {"closed": url, "title": pr.get("title")}


def repo_about(repo, description, topics, note):
    owner, _, name = repo.partition("/")
    if owner.lower() != _me() or not name:
        raise CommandError("only a repository under our own account can be described here")
    info = gh_json("repo", "view", repo, "--json", "isFork", timeout=GH_READ_TIMEOUT) or {}
    if info.get("isFork") is not False:
        raise CommandError("a fork carries its parent's identity; describe only our own projects")
    if description is None and not topics:
        raise CommandError("pass --description, --add-topic or both")
    invalid = [topic for topic in topics if not TOPIC.fullmatch(topic)]
    if invalid:
        raise CommandError("topics are lowercase letters, digits and hyphens, at most 50 characters: " + ", ".join(invalid))
    command = ["gh", "repo", "edit", repo]
    if description is not None:
        if not description.strip() or "\n" in description or len(description) > 350:
            raise CommandError("the description is one line of at most 350 characters")
        fails, _ = prose_findings("description", description)
        fails += leaks.scan(text=description, label="description")
        if fails:
            raise CommandError("the description fails the publication scan: " + "; ".join(privacy.redact(f) for f in fails))
        command += ["--description", description]
    for topic in topics:
        command += ["--add-topic", topic]
    run(command)
    _record("repo-about", repo, note, description=description, topics=list(topics))
    return {"described": repo, "description": description, "topics": list(topics)}


def _branches(repo):
    names, page = [], 1
    while True:
        batch = gh_json("api", f"repos/{repo}/branches?per_page={PAGE}&page={page}", timeout=GH_READ_TIMEOUT) or []
        names.extend(row["name"] for row in batch)
        if len(batch) < PAGE:
            return names
        page += 1


def fork_reasons(repo):
    """Why this fork must stay; empty when deleting it loses nothing the factory or the owner made."""
    owner, _, name = repo.partition("/")
    if owner.lower() != _me() or not name:
        return ["only a fork under our own account can be deleted here"]
    info = gh_json("api", f"repos/{repo}", timeout=GH_READ_TIMEOUT) or {}
    parent = info.get("parent") or {}
    if info.get("fork") is not True or not parent.get("full_name"):
        return ["not a fork: deleting one of our own projects is the owner's to do"]
    query = f"is:pr is:open author:{CONFIG['github_user']} repo:{parent['full_name']}"
    found = gh_json("api", "-X", "GET", "search/issues", "-f", f"q={query}", timeout=GH_READ_TIMEOUT) or {}
    if found.get("total_count") != 0:
        return [f"{found.get('total_count', 'an unknown number of')} open pull request(s) to {parent['full_name']} "
                "still depend on a fork; let them merge or close first"]
    reasons = []
    for branch in _branches(repo):
        if branch.startswith(FACTORY_BRANCHES):
            head = quote(f"{owner}:{branch}", safe=":")
            sent = gh_json("api", f"repos/{parent['full_name']}/pulls?head={head}&state=all&per_page=1",
                           timeout=GH_READ_TIMEOUT)
            if not sent:
                reasons.append(f"branch {branch} was pushed but never became a pull request; its commits may exist only in this fork")
            continue
        base = quote(parent["default_branch"], safe="")
        head = quote(f"{owner}:{branch}", safe=":")
        try:
            compared = gh_json("api", f"repos/{parent['full_name']}/compare/{base}...{head}", timeout=GH_READ_TIMEOUT) or {}
        except CommandError:
            compared = {}
        ahead = compared.get("ahead_by")
        if ahead != 0:
            count = f"{ahead} commit(s)" if isinstance(ahead, int) else "commits that could not be compared"
            reasons.append(f"branch {branch} holds {count} the parent does not have; that is the owner's own work to keep or discard")
    return reasons


def delete_fork(repo, note):
    reasons = fork_reasons(repo)
    if reasons:
        raise CommandError("; ".join(reasons))
    run(["gh", "repo", "delete", repo, "--yes"])
    _record("delete-fork", repo, note)
    return {"deleted": repo}


def _command(args):
    try:
        if args.owner_action == "close-pr":
            result = close_pr(args.url, args.note)
        elif args.owner_action == "repo-about":
            result = repo_about(args.repo, args.description, args.add_topic, args.note)
        elif args.dry_run:
            result = [{"fork": repo, "deletable": not reasons, "why": reasons}
                      for repo in args.repo for reasons in [fork_reasons(repo)]]
        else:
            result = []
            for repo in args.repo:
                try:
                    result.append(delete_fork(repo, args.note))
                except CommandError as error:
                    result.append({"kept": repo, "why": str(error)})
    except (CommandError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}, indent=2, ensure_ascii=False))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def register(sub):
    group = sub.add_parser("owner", help="run an account operation the owner delegated to the coordinator")
    actions = group.add_subparsers(dest="owner_action", required=True)
    close = actions.add_parser("close-pr", help="close one of our own open pull requests")
    close.add_argument("url")
    about = actions.add_parser("repo-about", help="set the description and topics of one of our own projects")
    about.add_argument("repo")
    about.add_argument("--description")
    about.add_argument("--add-topic", action="append", default=[])
    delete = actions.add_parser("delete-fork", help="delete forks that back no open pull request and hold no work of the owner's")
    delete.add_argument("repo", nargs="+")
    delete.add_argument("--dry-run", action="store_true")
    for parser in (close, about, delete):
        parser.add_argument("--note", required=True, help="the owner's instruction that delegates this, quoted")
        parser.set_defaults(fn=_command)
