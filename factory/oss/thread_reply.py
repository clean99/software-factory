"""Answer inside an inline review thread of the item's PR. The thread is named by its first comment's id,
never by line, because a push moves lines (L-059). The reply file is read once: the approval, the hygiene scan,
the publication guard and the post all see that one snapshot, so a later edit to the file is never published."""
import hashlib
import sys

from factory.core import publication
from factory.util import CONFIG, GH_READ_TIMEOUT, gh_json, gh_paginate, iso, read_json, split_ref


def approved_text(item, name):
    reply = item.file(name)
    approval = read_json(item.file(f"approval-{reply.stem}.json")) or {}
    try:
        data = publication.read_file(reply)
        text = data.decode("utf-8")
    except (OSError, ValueError):
        data = text = None
    if data is None or approval.get("sha256") != hashlib.sha256(data).hexdigest():
        raise SystemExit(f"{reply.name} not approved in its current form; show it to the user and run approve-reply --file {reply.name}")
    return reply.name, text


def pr_changes(meta, pr):
    changes = []
    if pr["state"] != "OPEN":
        changes.append(f"PR is {pr['state']} (expected OPEN)")
    expected = meta.get("pushed_head") or meta.get("head_sha")
    if not expected:
        changes.append("item has no recorded pushed_head or head_sha")
    elif pr["headRefOid"] != expected:
        changes.append(f"head changed from {expected} to {pr['headRefOid']}; someone else may have pushed")
    return changes


def thread_replies(repo, number, comment_id):
    comments = gh_paginate(f"repos/{repo}/pulls/{number}/comments?per_page=100")
    root = next((c for c in comments if c.get("id") == comment_id), None)
    if root is None:
        raise SystemExit(f"comment {comment_id} is not an inline review comment on {repo}#{number}")
    if root.get("in_reply_to_id"):
        raise SystemExit(f"comment {comment_id} is itself a reply; pass the thread's first comment, --comment-id {root['in_reply_to_id']}")
    return [c for c in comments if c.get("in_reply_to_id") == comment_id]


def own_reply(replies, body):
    me = CONFIG["github_user"].lower()
    return next((c.get("html_url") for c in replies
                 if ((c.get("user") or {}).get("login") or "").lower() == me and (c.get("body") or "").strip() == body), None)


def run(item, name, body, comment_id, force=False):
    meta = item.meta
    if not meta.get("pr_url"):
        raise SystemExit("item has no pr_url; thread replies go on an open PR")
    repo, number = split_ref(meta["pr_url"])
    pr = gh_json("pr", "view", meta["pr_url"], "--json", "state,headRefOid", timeout=GH_READ_TIMEOUT)
    changes = pr_changes(meta, pr)
    if changes:
        message = "; ".join(changes)
        if not force:
            raise SystemExit(f"thread reply refused: {message}. Re-read the PR before using --force")
        print(f"warning: --force overrides thread reply check: {message}", file=sys.stderr)
    url = own_reply(thread_replies(repo, number, comment_id), body)
    if not url:
        endpoint = f"repos/{repo}/pulls/{number}/comments/{comment_id}/replies"
        code = publication.run("gh", ["api", "-X", "POST", endpoint, "-f", f"body={body}", "--silent"], item.dir, item.dir)
        if code:
            raise SystemExit(f"thread reply not posted (exit {code}); nothing recorded")
        url = own_reply(thread_replies(repo, number, comment_id), body)
        if not url:
            raise SystemExit(f"reply sent to thread {comment_id} but not found in it; re-read the PR before running this again")
    meta.setdefault("thread_replies", []).append({"file": name, "comment_id": comment_id, "url": url, "at": iso()})
    item.save()
    return url
