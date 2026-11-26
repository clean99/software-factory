"""What the PR view does not show on an open PR: a fork branch the PR has not picked up (assistant-ui #10286
stayed on the old head for half an hour after a push, 2026-11-03), and unresolved inline threads opened by
review bots, where their real findings live while their summary comments are noise. Human inline threads are
read the same way: a community or maintainer review whose summary is empty keeps its substance there
(react-date-picker #743 lost two inline findings behind an empty COMMENTED review, 2026-11-24)."""
import re
from urllib.parse import quote

from factory.util import CONFIG, GH_READ_TIMEOUT, CommandError, gh_json, run

THREADS = ("query($owner:String!,$name:String!,$number:Int!,$after:String){repository(owner:$owner,name:$name){pullRequest(number:$number)"
           "{reviewThreads(first:100,after:$after){pageInfo{hasNextPage endCursor}"
           "nodes{isResolved comments(first:1){nodes{author{login __typename} authorAssociation databaseId path line originalLine body url}}"
           "last:comments(last:1){nodes{author{login __typename} authorAssociation databaseId body url}}}}}}}")
THREAD_PAGES = 20
COMMENT = re.compile(r"<!--.*?-->", re.S)
DETAILS = re.compile(r"<details\b[^>]*>(?:(?!<details\b).)*?</details\s*>", re.S | re.I)
PICTURE = re.compile(r"<picture\b[^>]*>.*?</picture\s*>", re.S | re.I)
SUMMARY = re.compile(r"<details\b.*?<summary\b[^>]*>(.*?)</summary\s*>", re.S | re.I)
TAG = re.compile(r"(`[^`\n]*`)|</?[a-z][a-z0-9-]*(?:\s[^>]*)?/?>", re.I)


def lines(r, number, pr, classify, emit=None, pending=None):
    """Action lines for one open PR; a failed read becomes a FOLLOWUP ERROR line and the other check still runs.
    classify(response) returns "bot", "maintainer" or "community". After a complete thread read, pending["inline"]
    holds the comment ids of human threads still waiting on us."""
    found = []
    for operation, check in (("fork-head", lambda: stale_head(r["url"], pr, emit)),
                             ("review-threads", lambda: thread_findings(r, number, classify, emit, pending))):
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


def review_threads(repo, number):
    """Every review thread of the PR, page by page; a missing page raises rather than returning a partial list."""
    owner, name = repo.split("/", 1)
    nodes, after = [], None
    for _ in range(THREAD_PAGES):
        args = ["api", "graphql", "-f", f"query={THREADS}", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}"]
        if after:
            args += ["-f", f"after={after}"]
        data = gh_json(*args, timeout=GH_READ_TIMEOUT)
        try:
            page = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes += page["nodes"]
        except (KeyError, TypeError):
            raise ValueError("review threads missing from the GraphQL response") from None
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return nodes
        after = info.get("endCursor")
        if not after:
            raise ValueError("review threads page has more results but no cursor")
    raise ValueError(f"review threads exceed {THREAD_PAGES} pages")


def thread_findings(r, number, classify, emit=None, pending=None):
    """Unresolved threads we did not answer last. Bot threads stay keyed by their first comment; a human thread is
    keyed by its newest comment, so a reviewer's follow-up in an answered thread is new feedback."""
    threads = review_threads(r["repo"], number)
    me = CONFIG["github_user"].lower()
    found, waiting, bots = [], [], []
    for thread in threads:
        first = next(iter((thread.get("comments") or {}).get("nodes") or []), None)
        if thread.get("isResolved") or not first or last_author(thread) == me:
            continue
        kind = classify(first)
        login = (first.get("author") or {}).get("login") or "[deleted]"
        line_no = first.get("line") or first.get("originalLine")
        where = f"{first.get('path')}:{line_no}"
        if kind == "bot":
            text = finding_text(first.get("body") or "")[:160]
            line = f"BOT FINDING {r['url']} by {login} {where} [comment {first.get('databaseId')}]: {text}"
            found.append(line)
            bots.append(first.get("databaseId"))
            if emit:
                emit("bot-finding", first.get("databaseId"), {"text": line, "body": finding_text(first.get("body") or ""),
                                                           "path": first.get("path"), "line": line_no})
            continue
        newest = next(iter((thread.get("last") or {}).get("nodes") or []), None) or first
        if classify(newest) == "bot":
            newest = first  # a bot's note in a human thread does not replace the reviewer's words
        # A maintainer anywhere in the thread makes it maintainer feedback.
        kind = "maintainer" if "maintainer" in {kind, classify(newest)} else "community"
        author = (newest.get("author") or {}).get("login") or "[deleted]"
        body = newest.get("body") or ""
        line = (f"{kind.upper()} INLINE {r['url']} by {author} {where} [comment {newest.get('databaseId')}]: "
                f"{' '.join(body.split())[:160]!r}")
        found.append(line)
        waiting.append(newest.get("databaseId"))
        if emit:
            emit(f"{kind}-inline", newest.get("databaseId"),
                 {"text": line, "body": body, "author_login": author, "path": first.get("path"),
                  "line": line_no, "comment_id": newest.get("databaseId"),
                  "thread_comment_id": first.get("databaseId"), "comment_url": newest.get("url") or first.get("url"),
                  "thread_body": (first.get("body") or "") if newest is not first else ""})
    if pending is not None:
        pending["inline"], pending["bot"] = waiting, bots
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
