"""Is this issue still worth a PR from us right now?

Encodes the collision lessons: an assignee is a claim, any open PR is a collision, a closed outside
attempt is a reason to read why it was closed, and a recent "I'll take this" comment counts.
Run before coding and again right before submit; results older than the configured window are
treated as unknown by the gate.
"""
import json
import re

from factory.util import CONFIG, gh_json, gh_paginate, git, hours_since, iso, issue_number, parse_iso

CORE = {"OWNER", "MEMBER", "COLLABORATOR"}
CLAIM = re.compile(
    r"\b(i('| a)?m working on|i('|’)ll (take|work on|fix|pick)|i (can|could|would like to|want to) (take|work on|fix|pick)|assign (it )?(to )?me|can i (take|work on)|working on (this|it)|i('|’)d like to (take|work on|fix))\b|我来|分配给我|认领",
    re.I,
)
CONTRIBUTE = re.compile(
    r"^(?:yes[,!]?\s+)?(?:i\s+(?:would\s+like\s+to|want\s+to|can|could|will|am\s+willing\s+to|"
    r"am\s+happy\s+to|would\s+be\s+happy\s+to)|i['’]d\s+like\s+to|i['’]ll|i['’]m\s+(?:willing|happy)\s+to|happy\s+to)\s+"
    r"(?:(?:submit|open|send|raise|make|create)\s+(?:a\s+|the\s+)?(?:pr|pull\s+request|fix|patch)|"
    r"(?:work\s+on|fix|take)\s+(?:this|it|the\s+(?:issue|bug|fix)))\b",
    re.I,
)
MAINTAINER_TAKES = re.compile(r"^(?:(?:we['’]ll|i['’]ll|i will|we will|i['’]m) (?:fix|handle|look into|take care|address)\b|fixed (?:in|by)\b|duplicate of\b)", re.I)
LEGACY_CLAIM = re.compile(r"^(?:" + CLAIM.pattern + ")", re.I)
WELCOME_LABEL = re.compile(r"good first issue|help wanted|contributions? welcome|pr welcome|accepting prs|up for grabs|easy", re.I)
STOP_LABEL = re.compile(r"wontfix|won't fix|invalid|duplicate|not planned|stale|needs[- ]design|blocked|on hold|in progress|in-progress", re.I)
BOT_AUTHORS = re.compile(r"\[bot\]$|^(dependabot|renovate|github-actions|mergify|copilot-swe-agent)", re.I)


WORK_CLAIM = re.compile(
    r"^(?:i(?:['’]m| am)\s+(?:already\s+)?(?:working\s+on|coordinating\b)|"
    r"i(?:['’]ll| will)\s+(?:take|pick\s+up|work\s+on|fix)\s+(?:this|it|the\s+(?:issue|bug))\b|"
    r"(?:i(?:['’]m| am)\s+)?working\s+on\s+(?:this|it|a\s+fix|the\s+fix)\b|pr\s+incoming\b)", re.I)
FIXED = re.compile(
    r"^(?:(?:this|it|that|the\s+(?:issue|bug|fix))\s+(?:(?:is|was)\s+|has\s+been\s+))?"
    r"(?:already\s+)?(?:fixed|resolved|released|shipped)\b|"
    r"^(?:the\s+|upstream\s+)?fix\s+(?:is|has\s+been)\s+(?:up|merged|released|shipped)\b", re.I)
REFERENCE = re.compile(
    r"https://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/(?:pull|issues)/(?P<url_num>[1-9]\d*)\b|"
    r"(?<![\w./-])(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<num>[1-9]\d*)\b", re.I)


def sentences(body):
    """Visible assertions, excluding quoted examples and unchecked template options."""
    fence = None
    for line in re.sub(r"<!--.*?-->", "", body or "", flags=re.S).splitlines():
        text = line.strip()
        marker = re.match(r"(`{3,}|~{3,})", text)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            continue
        if fence or text.startswith(">"):
            continue
        text = re.sub(r"^[-*+]\s+", "", text)
        checkbox = re.match(r"\[([ xX])\]\s*(.*)", text)
        if checkbox:
            if checkbox[1] == " ":
                continue
            text = checkbox[2]
        yield from re.split(r"(?<=[.!?])\s+", text)


INTRODUCTION = re.compile(
    r"^(?:(?:(?:hi|hey|hello)(?:\s+(?:all|everyone|folks))?|"
    r"thanks(?:\s+for\s+(?:reporting(?:\s+this)?|the\s+report))?|"
    r"(?:quick\s+)?update|fyi|fwiw)\s*[,!:]\s*)+", re.I)


def assertion(body, pattern):
    # Strip only harmless introductions, not arbitrary clauses containing a quoted claim.
    return next((s for s in sentences(body) if not s.endswith("?") and
                 pattern.search(INTRODUCTION.sub("", s))), None)


def contribution_intent(body):
    """Quote a first-person offer, not an unchecked template option, example or invitation."""
    return assertion(body, CONTRIBUTE)


def closing_quote(body, source_repo, repo, num):
    from factory.core.gate import closing_references

    for sentence in sentences(body):
        for match in closing_references(sentence):
            if int(match["number"]) == num and (match["url_repo"] or match["repo"] or source_repo).lower() == repo.lower():
                return sentence
    return None


def fix_links(body):
    return [s for s in sentences(body) if not s.endswith("?") and
            (assertion(s, FIXED) or re.search(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+|\b(?:pr|pull request)\s+#\d+", s, re.I))]


def linked_fixes(repo, num, quotes):
    """Resolve fixing links in one read, including PRs merged before the issue existed."""
    targets = {}
    for quote in quotes:
        for match in REFERENCE.finditer(quote):
            key = (match["url_repo"] or match["repo"] or repo, int(match["url_num"] or match["num"]))
            if key not in targets or assertion(quote, FIXED):
                targets[key] = quote
    if not targets:
        return []
    fields = []
    for index, (source_repo, number) in enumerate(targets):
        owner, name = source_repo.split("/")
        fields.append(f'p{index}:repository(owner:{json.dumps(owner)},name:{json.dumps(name)})'
                      f'{{issueOrPullRequest(number:{number}){{__typename ... on PullRequest{{number title url state merged body author{{login}} createdAt '
                      'closingIssuesReferences(first:100){nodes{number repository{nameWithOwner}}}}}}')
    data = gh_json("api", "graphql", "-f", "query=query{" + " ".join(fields) + "}")
    if data.get("errors"):
        raise ValueError("linked PR lookup failed; qualification is incomplete")
    result = []
    for index, ((source_repo, _), quote) in enumerate(targets.items()):
        repository = data["data"].get(f"p{index}")
        pr = (repository or {}).get("issueOrPullRequest")
        if not pr or pr.get("__typename") != "PullRequest":
            continue
        evidence = quote if assertion(quote, FIXED) else closing_quote(pr.get("body"), source_repo, repo, num)
        closing = (pr.get("closingIssuesReferences") or {}).get("nodes", [])
        if not evidence and any(node["number"] == num and node["repository"]["nameWithOwner"].lower() == repo.lower() for node in closing):
            evidence = f"GitHub closing reference: {repo}#{num}"
        if evidence:
            entry = search_pr(pr, source_repo, pr["state"].lower(), via="fix-link")
            entry.update(same_repo=source_repo.lower() == repo.lower(), fix_quote=evidence)
            result.append(entry)
    return result


def references_issue(text, repo, num):
    """True when text points at issue `num` of `repo` itself, not at the same number in another repo."""
    owner_repo = re.escape(repo)
    pattern = (
        rf"(?<![\w./-])#{num}\b"
        rf"|(?<![\w.-]){owner_repo}#{num}\b"
        rf"|github\.com/{owner_repo}/(issues|pull|discussions)/{num}\b"
    )
    return bool(re.search(pattern, text or "", re.I))


TRIAGE_LABEL = re.compile(r"triage|needs[- ]repro|needs[- ]info|question|unconfirmed", re.I)


DISCUSSION = """query($o:String!,$n:String!,$num:Int!){repository(owner:$o,name:$n){discussion(number:$num){
id title url closed createdAt updatedAt authorAssociation author{login} labels(first:20){nodes{name}}
comments(first:100){nodes{author{login} authorAssociation createdAt body}}}}}"""


PR_FIELDS = "number,title,author,url,body,createdAt"


def search_pr(pr, repo, state="open", via="search"):
    return {"number": pr["number"], "state": state, "merged": state == "merged", "author": pr["author"]["login"],
            "same_repo": True, "repo": repo, "url": pr["url"], "title": pr["title"], "created_at": pr["createdAt"], "via": via}


def run_discussion(repo, num, keywords=()):
    me = CONFIG["github_user"].lower()
    owner, name = repo.split("/")
    d = gh_json("api", "graphql", "-f", f"query={DISCUSSION}", "-F", f"o={owner}", "-F", f"n={name}", "-F", f"num={num}")["data"]["repository"]["discussion"]
    skip, warn, plus = [], [], []
    if d["closed"]:
        skip.append("discussion is closed")
    for c in d["comments"]["nodes"]:
        who = (c.get("author") or {}).get("login", "")
        if who.lower() == me:
            continue
        days = hours_since(c["createdAt"]) / 24
        body = c["body"] or ""
        if c["authorAssociation"] in CORE and re.search(r"pr(s)? (welcome|appreciated)|feel free to (open|submit)|contributions? welcome", body, re.I):
            plus.append(f"maintainer {who} invited PRs {days:.0f}d ago")
        elif c["authorAssociation"] in CORE:
            warn.append(f"maintainer {who} commented {days:.0f}d ago: {body.strip()[:100]!r}")
        elif CLAIM.search(body) and days < 30:
            skip.append(f"{who} claimed it {days:.0f}d ago: {body.strip()[:80]!r}")
    prs = {}
    for keyword in [str(num), *keywords]:
        for pr in gh_json("pr", "list", "-R", repo, "--state", "open", "--search", keyword, "--json", PR_FIELDS, "--limit", "1000") or []:
            if pr["author"]["login"].lower() != me:
                warn.append(f"open PR matches '{keyword}': #{pr['number']} {pr['title'][:70]}")
                if keyword != str(num) or references_issue(pr["title"] + "\n" + (pr.get("body") or ""), repo, num):
                    prs[pr["url"]] = search_pr(pr, repo)
    if not plus:
        warn.append("no maintainer invitation yet: proposal first")
    return {
        "verdict": "skip" if skip else "ok", "at": iso(), "repo": repo, "issue": num, "target": "discussion",
        "node_id": d["id"], "title": d["title"], "url": d["url"], "labels": [l["name"] for l in d["labels"]["nodes"]],
        "author": d["author"]["login"], "author_association": d["authorAssociation"],
        "age_days": round(hours_since(d["createdAt"]) / 24), "idle_days": round(hours_since(d["updatedAt"]) / 24),
        "comments": len(d["comments"]["nodes"]), "skip": skip, "warn": warn, "plus": plus, "prs": list(prs.values()),
    }


def run_keywords(repo, keywords):
    """Items without an issue (e.g. a new plugin the repo invites directly): collisions by search."""
    me = CONFIG["github_user"].lower()
    skip, warn, prs = [], [], {}
    if not keywords:
        skip.append("no issue and no keywords: give --keyword terms that a competing PR would contain")
    for keyword in keywords:
        for state in ("open", "merged"):
            found = gh_json("pr", "list", "-R", repo, "--state", state, "--search", keyword, "--json", PR_FIELDS, "--limit", "1000") or []
            for pr in found:
                if pr["author"]["login"].lower() == me:
                    continue
                (skip if state == "open" else warn).append(f"{state} PR matches '{keyword}': #{pr['number']} {pr['title'][:70]}")
                prs[pr["url"]] = search_pr(pr, repo, state)
        issues = gh_json("issue", "list", "-R", repo, "--state", "open", "--search", f"{keyword} in:title", "--json", "number,title,assignees", "--limit", "10") or []
        for i in issues:
            warn.append(f"open issue matches '{keyword}': #{i['number']} {i['title'][:70]}" + (" (assigned)" if i["assignees"] else ""))
    return {"verdict": "skip" if skip else "ok", "at": iso(), "repo": repo, "issue": None, "target": "none",
            "keywords": list(keywords), "skip": skip, "warn": warn, "plus": [], "prs": list(prs.values())}


def classify_prs(prs, me):
    """Upstream PRs decide; an open PR in a fork of the repo is someone's work in progress, so a
    fresh one counts as a claim. PRs in unrelated repos only mention the issue."""
    skip, warn, outsiders = [], [], set()
    for pr in prs:
        if pr["author"].lower() == me and not pr["merged"]:
            continue
        quote = pr.get("fix_quote")
        if quote and (pr["merged"] or pr["state"] == "open"):
            state = "merged" if pr["merged"] else "open"
            skip.append(f"{state} PR {pr['url']} claims this fix: {quote!r}")
        elif not pr["same_repo"]:
            if pr["state"] != "open":
                continue
            where = f"{pr['repo']} #{pr['number']} by {pr['author']}"
            if not pr.get("fork"):
                warn.append(f"referenced from another project's PR: {where}")
            elif pr.get("updated_at") and hours_since(pr["updated_at"]) / 24 < 30:
                skip.append(f"work in progress in a fork: {where}")
            else:
                warn.append(f"stale PR in a fork: {where}")
        elif pr["merged"]:
            skip.append(f"merged PR #{pr['number']} already references it")
        elif pr["state"] == "open":
            skip.append(f"open PR #{pr['number']} by {pr['author']}")
        elif pr.get("author_association") in CORE or BOT_AUTHORS.search(pr["author"]):
            # A maintainer's own closed attempt (or their bot's) is a design signal, not a swarm:
            # sveltejs/kit #17482 had both, with the maintainer naming the approach on closing.
            warn.append(f"closed maintainer-side attempt #{pr['number']} by {pr['author']}: follow what they said on closing")
        else:
            outsiders.add(pr["author"])
    return skip, warn, outsiders


def run(repo, num, keywords=(), target="issue"):
    if target == "none" or num is None:
        return run_keywords(repo, keywords)
    if target == "discussion":
        return run_discussion(repo, num, keywords)
    num = issue_number(num, repo)
    me = CONFIG["github_user"].lower()
    skip, warn, plus = [], [], []
    issue = gh_json("api", f"repos/{repo}/issues/{num}")
    if "pull_request" in issue:
        return {"verdict": "skip", "skip": ["this number is a PR, not an issue"], "at": iso()}

    reporter = issue["user"]["login"]
    intent = contribution_intent(issue.get("body"))
    if reporter.lower() != me and intent:
        skip.append(f"reporter {reporter} intends to contribute (issue body): {intent!r}")
    labels = [l["name"] for l in issue["labels"]]
    assignees = [a["login"] for a in issue["assignees"]]
    if issue["state"] != "open":
        skip.append(f"issue is {issue['state']} ({issue.get('state_reason')})")
    others = [a for a in assignees if a.lower() != me]
    if others:
        skip.append(f"assigned to {', '.join(repr(a) for a in others)} ({issue['html_url']})")
    for l in labels:
        if STOP_LABEL.search(l):
            skip.append(f"label '{l}'")
        elif WELCOME_LABEL.search(l):
            plus.append(f"label '{l}'")
        elif TRIAGE_LABEL.search(l):
            warn.append(f"label '{l}': not yet confirmed by maintainers")
    if issue["author_association"] in CORE:
        warn.append(f"filed by {issue['author_association']} {issue['user']['login']}: core may already plan to handle it")
    age_days = hours_since(issue["created_at"]) / 24
    idle_days = hours_since(issue["updated_at"]) / 24
    if idle_days > 180:
        warn.append(f"no activity for {idle_days:.0f} days")

    timeline = gh_paginate(f"repos/{repo}/issues/{num}/timeline")
    prs = {}
    links = fix_links(issue.get("body"))
    for ev in timeline:
        kind = ev.get("event")
        if kind == "cross-referenced":
            src = ev.get("source", {}).get("issue", {})
            if not src.get("pull_request"):
                continue
            source_repo = src.get("repository", {})
            same_repo = source_repo.get("full_name", repo) == repo
            author = src["user"]["login"]
            merged = bool(src["pull_request"].get("merged_at"))
            prs[src.get("html_url") or src["number"]] = {
                "number": src["number"], "state": src["state"], "merged": merged, "author": author,
                "author_association": src.get("author_association", ""),
                "same_repo": same_repo, "url": src.get("html_url"), "title": src.get("title"),
                "repo": source_repo.get("full_name", repo), "updated_at": src.get("updated_at"), "created_at": src.get("created_at"),
                "fork": bool(source_repo.get("fork")) and source_repo.get("name") == repo.split("/")[1],
                "fix_quote": closing_quote(src.get("body"), source_repo.get("full_name", repo), repo, num),
            }
        elif kind == "commented":
            body = ev.get("body") or ""
            who = ev.get("user", {}).get("login") or ev.get("actor", {}).get("login", "")
            if who.lower() == me:
                continue
            assoc = ev.get("author_association", "")
            days = hours_since(ev["created_at"]) / 24
            intent = contribution_intent(body)
            claim = assertion(body, WORK_CLAIM)
            fixed = assertion(body, FIXED)
            maintainer_claim = assertion(body, MAINTAINER_TAKES)
            older_claim = assertion(body, LEGACY_CLAIM)
            links.extend(fix_links(body))
            if claim:
                skip.append(f"{who} claimed the work (comment, {days:.0f}d ago): {claim!r}")
            elif assoc in CORE and fixed:
                skip.append(f"maintainer {who} reports it fixed or released: {fixed!r}")
            elif intent and (who.lower() == reporter.lower() or assoc in CORE or days < 30):
                skip.append(f"{who} intends to contribute (comment, {days:.0f}d ago): {intent!r}")
            elif intent:
                warn.append(f"{who} offered to contribute {days:.0f}d ago (stale claim; consider asking): {intent!r}")
            elif assoc in CORE and maintainer_claim:
                skip.append(f"maintainer {who} {days:.0f}d ago: {maintainer_claim!r}")
            elif assoc in CORE and re.search(r"pr(s)? (welcome|appreciated)|feel free to (open|submit)|contributions? welcome", body, re.I):
                plus.append(f"maintainer {who} invited PRs {days:.0f}d ago")
            elif older_claim and days < 30:
                skip.append(f"{who} claimed it {days:.0f}d ago: {older_claim!r}")
            elif older_claim:
                warn.append(f"{who} claimed it {days:.0f}d ago (stale claim; consider asking): {older_claim!r}")
        elif kind == "connected":
            skip.append(f"GitHub closing reference: 'connected' via the development sidebar ({issue['html_url']})")

    for pr in linked_fixes(repo, num, links):
        prs[pr["url"]] = pr

    for keyword in [str(num), *keywords]:
        found = gh_json("pr", "list", "-R", repo, "--state", "open", "--search", keyword, "--json", PR_FIELDS, "--limit", "1000") or []
        for pr in found:
            author = pr["author"]["login"]
            if author.lower() == me or BOT_AUTHORS.search(author) or pr["url"] in prs:
                continue
            if keyword == str(num):
                if references_issue(pr["title"] + "\n" + (pr.get("body") or ""), repo, num):
                    prs[pr["url"]] = {**search_pr(pr, repo), "fix_quote": closing_quote(pr.get("body"), repo, repo, num)}
            else:
                warn.append(f"open PR matches '{keyword}': #{pr['number']} {pr['title'][:70]}")
                prs[pr["url"]] = search_pr(pr, repo, via="keyword")

    pr_skip, pr_warn, outsiders = classify_prs([p for p in prs.values() if p.get("via") != "keyword"], me)
    skip += pr_skip
    warn += pr_warn
    # A count of closed attempts proves nothing about the issue (owner, 2026-11-22); what each closing said decides.
    if outsiders:
        warn.append(f"closed attempts by {', '.join(sorted(outsiders))}: read why each was closed before starting, and drop "
                    "the issue only on an actual reason (already fixed, duplicate, claimed, or the direction was rejected)")

    verdict = "skip" if skip else "ok"
    return {
        "verdict": verdict, "at": iso(), "repo": repo, "issue": num, "title": issue["title"],
        "url": issue["html_url"], "labels": labels, "assignees": assignees,
        "author": issue["user"]["login"], "author_association": issue["author_association"],
        "age_days": round(age_days), "idle_days": round(idle_days), "comments": issue["comments"],
        "skip": skip, "warn": warn, "plus": plus, "prs": list(prs.values()),
    }


def fresh_collisions(item, since):
    """Uncached qualification plus file overlap, run after push so hooks cannot age the result."""
    cutoff = parse_iso(since)
    meta, me = item.meta, CONFIG["github_user"].lower()
    repo = meta["repo"]
    result = run(repo, meta.get("issue"), meta.get("keywords", []), meta.get("target", "issue"))
    candidates = {p["url"]: p for p in result.get("prs", []) if p.get("url")}
    changed = set(git(item.clone, "diff", "--name-only", f"{item.merge_base()}..{meta['branch']}").splitlines())
    for pr in gh_paginate(f"repos/{repo}/pulls?state=open&per_page=100"):
        author = pr["user"]["login"]
        if author.lower() == me or BOT_AUTHORS.search(author) or parse_iso(pr["created_at"]) <= cutoff:
            continue
        if pr["html_url"] in candidates:
            continue
        files = gh_paginate(f"repos/{repo}/pulls/{pr['number']}/files?per_page=100")
        paths = {f[key] for f in files for key in ("filename", "previous_filename") if f.get(key)}
        overlap = sorted(changed & paths)
        if overlap:
            candidates[pr["html_url"]] = {
                "number": pr["number"], "url": pr["html_url"], "title": pr["title"], "author": author,
                "state": "open", "same_repo": True, "repo": repo, "created_at": pr["created_at"],
                "via": "files", "files": overlap,
            }
    collisions = []
    for pr in candidates.values():
        if pr["state"] != "open" or pr["author"].lower() == me or BOT_AUTHORS.search(pr["author"]):
            continue
        if not pr["same_repo"] and not pr.get("fork"):
            continue
        if not pr.get("created_at"):
            data = gh_json("api", f"repos/{pr['repo']}/pulls/{pr['number']}")
            pr.update(created_at=data["created_at"], state=data["state"])
        if pr["state"] == "open" and parse_iso(pr["created_at"]) > cutoff:
            collisions.append(pr)
    if collisions:
        result["verdict"] = "skip"
        result.setdefault("skip", []).extend(f"new competing PR {p['url']} (created {p['created_at']}, via {p.get('via', 'timeline')})" for p in collisions)
    result.update(since=since, collisions=collisions)
    return result
