"""What the PR view does not show on an open PR: a fork branch the PR has not picked up (assistant-ui #10286
stayed on the old head for half an hour after a push, 2026-11-03), and unresolved inline threads opened by
review bots, where their real findings live while their summary comments are noise."""
import re
from urllib.parse import quote

from factory.util import CONFIG, GH_READ_TIMEOUT, CommandError, gh_json, run

THREADS = ("query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number)"
           "{reviewThreads(first:100){nodes{isResolved comments(first:1){nodes{author{login __typename} databaseId path line originalLine body}}"
           "last:comments(last:1){nodes{author{login}}}}}}}}")
COMMENT = re.compile(r"<!--.*?-->", re.S)
DETAILS = re.compile(r"<details\b[^>]*>(?:(?!<details\b).)*?</details\s*>", re.S | re.I)
PICTURE = re.compile(r"<picture\b[^>]*>.*?</picture\s*>", re.S | re.I)
SUMMARY = re.compile(r"<details\b.*?<summary\b[^>]*>(.*?)</summary\s*>", re.S | re.I)
TAG = re.compile(r"(`[^`\n]*`)|</?[a-z][a-z0-9-]*(?:\s[^>]*)?/?>", re.I)


def lines(r, number, pr, is_bot, emit=None):
    """Action lines for one open PR; a failed read becomes a FOLLOWUP ERROR line and the other check still runs."""
    found = []
    for operation, check in (("fork-head", lambda: stale_head(r["url"], pr, emit)),
                             ("review-threads", lambda: bot_findings(r, number, is_bot, emit))):
        try:
            found += check()
        except Exception as error:
            text = f"FOLLOWUP ERROR {r['url']}: {' '.join(str(error).split())}"
            found.append(text)
            if emit:
                emit("followup-error", f"{operation}:{pr.get('headRefOid') or 'no-head'}", {"text": text, "operation": operation})
    return found


def fork_head(pr):
    """None when the PR's fork or branch is gone; then there is nothing to compare."""
    owner = (pr.get("headRepositoryOwner") or {}).get("login")
    name = (pr.get("headRepository") or {}).get("name")
    branch = pr.get("headRefName")
    if not (owner and name and branch):
        return None
    try:
        sha = run(["gh", "api", f"repos/{owner}/{name}/branches/{quote(branch)}", "--jq", ".commit.sha"],
                  timeout=GH_READ_TIMEOUT).stdout.strip()
    except CommandError as error:
        if "HTTP 404" in str(error):
            return None
        raise
    if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise ValueError(f"fork branch {owner}/{name}:{branch} gave no commit sha")
    return sha


def stale_head(url, pr, emit=None):
    fork = fork_head(pr)
    if not fork:
        return []
    shown = pr.get("headRefOid")
    if not shown:
        raise ValueError("PR view has no headRefOid to compare with the fork branch")
    if fork == shown:
        return []
    text = (f"STALE PR HEAD {url}: fork branch at {fork[:9]}, PR shows {shown[:9]}; "
            "re-push, or close and reopen the PR to resync, then confirm")
    if emit:
        emit("stale-pr-head", fork, {"text": text, "fork_head": fork, "pr_head": shown})
    return [text]


def bot_findings(r, number, is_bot, emit=None):
    owner, name = r["repo"].split("/", 1)
    data = gh_json("api", "graphql", "-f", f"query={THREADS}", "-f", f"owner={owner}", "-f", f"name={name}",
                   "-F", f"number={number}", timeout=GH_READ_TIMEOUT)
    try:
        threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (KeyError, TypeError):
        raise ValueError("review threads missing from the GraphQL response") from None
    me = CONFIG["github_user"].lower()
    found = []
    for thread in threads:
        first = next(iter((thread.get("comments") or {}).get("nodes") or []), None)
        if thread.get("isResolved") or not first or not is_bot(first) or last_author(thread) == me:
            continue
        login = (first.get("author") or {}).get("login") or "[deleted]"
        where = f"{first.get('path')}:{first.get('line') or first.get('originalLine')}"
        text = finding_text(first.get("body") or "")[:160]
        line = f"BOT FINDING {r['url']} by {login} {where} [comment {first.get('databaseId')}]: {text}"
        found.append(line)
        if emit:
            emit("bot-finding", first.get("databaseId"), {"text": line, "body": finding_text(first.get("body") or ""),
                                                       "path": first.get("path"), "line": first.get("line") or first.get("originalLine")})
    return found


def last_author(thread):
    """Lower-case login of a thread's newest comment. A thread we answered last waits on the bot or a
    maintainer, not on us (Rollup #6628 comment 4467905425 kept coming back after our reply, 2026-11-05)."""
    last = next(iter((thread.get("last") or {}).get("nodes") or []), None)
    return ((last or {}).get("author") or {}).get("login", "").lower()


def without_markup(text):
    """Badges and HTML tags out; inline code such as `Map<K, V>` stays as written."""
    return TAG.sub(lambda m: m.group(1) or " ", PICTURE.sub(" ", text))


def finding_text(body):
    """A bot comment's own words. CodeRabbit wraps its title in a badge line and buries scripts, diffs and agent
    prompts in <details> blocks; a body that is nothing but <details> falls back to its first <summary> line."""
    body = COMMENT.sub(" ", body)
    text, removed = body, 1
    while removed:
        text, removed = DETAILS.subn(" ", text)
    text = " ".join(without_markup(text).split())
    if text:
        return text
    summary = SUMMARY.search(body)
    lines = without_markup(summary.group(1)).splitlines() if summary else []
    return " ".join(next((line for line in lines if line.strip()), "").split())
