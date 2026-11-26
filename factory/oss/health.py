"""Is this repo a good place to spend effort?

Three numbers decide most of it: how many outsiders actually get merged, how fast, and how
flooded the repo is with outsider PRs that die unmerged (the AI-slop pressure on maintainers).
"""
import datetime as dt

from factory.oss import records
from factory.util import CONFIG, gh_json, iso, now, parse_iso

CORE = {"OWNER", "MEMBER", "COLLABORATOR"}
QUERY = """query($q:String!,$cursor:String){search(query:$q,type:ISSUE,first:100,after:$cursor){issueCount pageInfo{hasNextPage endCursor}
nodes{... on PullRequest{number authorAssociation createdAt mergedAt closedAt author{login __typename} mergedBy{login}}}}}"""


def search(q, pages=2):
    nodes, cursor, total = [], None, 0
    for _ in range(pages):
        args = ["api", "graphql", "-f", f"q={q}", "-f", f"query={QUERY}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        data = gh_json(*args)["data"]["search"]
        total = data["issueCount"]
        nodes += [n for n in data["nodes"] if n]
        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]
    return total, nodes


def mergers(*groups):
    """Logins that merged PRs here: team members whose org membership is private still show up as CONTRIBUTOR."""
    return {((n.get("mergedBy") or {}).get("login") or "").lower() for g in groups for n in g} - {""}


def outsider(n, team=frozenset()):
    author = n.get("author") or {}
    return author.get("__typename") == "User" and n["authorAssociation"] not in CORE and author.get("login", "").lower() not in team


def run(repo, days=30, me=None):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    meta = gh_json("api", f"repos/{repo}")
    merged_total, merged = search(f"repo:{repo} is:pr is:merged merged:>={since}")
    _, created = search(f"repo:{repo} is:pr created:>={since}", pages=3)

    team = mergers(merged, created)
    ext_merged = [n for n in merged if outsider(n, team)]
    hours = sorted((parse_iso(n["mergedAt"]) - parse_iso(n["createdAt"])).total_seconds() / 3600 for n in ext_merged)
    authors = {}
    for n in ext_merged:
        authors[n["author"]["login"]] = authors.get(n["author"]["login"], 0) + 1

    ext_created = [n for n in created if outsider(n, team)]
    ec_merged = [n for n in ext_created if n["mergedAt"]]
    ec_closed = [n for n in ext_created if n["closedAt"] and not n["mergedAt"]]
    ec_open = [n for n in ext_created if not n["closedAt"]]
    resolved = len(ec_merged) + len(ec_closed)
    rejection = round(len(ec_closed) / resolved, 2) if resolved else None
    backlog = round(len(ec_open) / len(ext_created), 2) if ext_created else None
    newcomers = [n for n in ext_created if n["authorAssociation"] in ("FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE")]

    out = {
        "repo": repo, "at": iso(), "stars": meta["stargazers_count"], "archived": meta["archived"],
        "default_branch": meta["default_branch"], "window_days": days,
        "merged_prs": merged_total, "sampled_merged": len(merged),
        "outsider_merged_in_sample": len(ext_merged),
        "distinct_outsider_authors": len(authors),
        "one_off_outsider_authors": sum(1 for v in authors.values() if v == 1),
        "median_hours_to_merge_outsider": round(hours[len(hours) // 2]) if hours else None,
        "outsider_prs_created": len(ext_created), "outsider_created_merged": len(ec_merged),
        "outsider_created_closed_unmerged": len(ec_closed), "outsider_created_open": len(ec_open),
        "outsider_rejection_rate": rejection, "outsider_backlog_share": backlog,
        "newcomer_prs_created": len(newcomers),
        "flood_index": rejection,
        "caveat": "authorAssociation is the current one, so merged newcomers count as CONTRIBUTOR; core members with private org membership look like outsiders (check the repo's team list)",
    }
    if me:
        _, mine = search(f"repo:{repo} is:pr author:{me}", pages=1)
        out["my_prs"] = {
            "open": sum(1 for n in mine if not n["closedAt"]),
            "merged": sum(1 for n in mine if n["mergedAt"]),
            "closed_unmerged": sum(1 for n in mine if n["closedAt"] and not n["mergedAt"]),
        }
    return out


RECORD_QUERY = """query($q:String!){search(query:$q,type:ISSUE,first:50){issueCount nodes{... on PullRequest{
number createdAt mergedAt authorAssociation author{login __typename} mergedBy{login}
comments(first:20){totalCount nodes{authorAssociation createdAt author{login __typename}}}
reviews(first:20){totalCount nodes{authorAssociation submittedAt author{login __typename}}}
timelineItems(first:5,itemTypes:[CLOSED_EVENT]){nodes{... on ClosedEvent{createdAt actor{login __typename}}}}}}}}"""


def utc_text(moment):
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def truncated(connection):
    """Whether GitHub holds more comments or reviews than the query returned."""
    return (connection.get("totalCount") or 0) > len(connection.get("nodes") or [])


def responsiveness(repo, at=None, refresh=False):
    """The long outsider-PR record behind the first-PR test (owner, 2026-11-22), in one GraphQL query.

    It reads up to 50 PRs opened between responsiveness_record_days and unresponsive_days ago, so each has had the
    full window to be answered, and counts outside contributors' PRs that a maintainer answered within
    unresponsive_days: a merge, a review or comment by a core member or a merger, or a close by someone other than
    the author and not a bot. Only at least responsiveness_min_outside_prs outside PRs from at least
    responsiveness_min_outside_authors authors, with fewer than responsiveness_min_response_percent answered, make
    the repository unresponsive; a smaller sample is not enough history to reject it. Only the first 20 comments and
    reviews of each PR are read: a PR with more and no answer among them is unknown, and an unknown PR counts as
    answered when deciding a rejection; a PR GitHub dates less than unresponsive_days ago is left out. The result is saved to the repository
    record and reused for responsiveness_record_max_age_days while the settings that produced it are unchanged;
    refresh reads it again."""
    limits = CONFIG["limits"]
    days = limits.get("unresponsive_days", 30)
    record = limits.get("responsiveness_record_days", 180)
    least = limits.get("responsiveness_min_outside_prs", 10)
    people = limits.get("responsiveness_min_outside_authors", 5)
    share = limits.get("responsiveness_min_response_percent", 20) / 100
    at = at or now()
    settings = {"record_days": record, "response_days": days, "min_outside_prs": least, "min_outside_authors": people,
                "min_response_share": share}
    saved = records.load(repo, "responsiveness")
    max_age = dt.timedelta(days=limits.get("responsiveness_record_max_age_days", 30))
    if not refresh and records.fresh(saved, max_age, at=at, settings=settings):
        return saved
    first, last = at - dt.timedelta(days=record), at - dt.timedelta(days=days)
    first_text, last_text = utc_text(first), utc_text(last)
    query = f"repo:{repo} is:pr created:{first_text}..{last_text} sort:created-desc"
    data = gh_json("api", "graphql", "-f", f"q={query}", "-f", f"query={RECORD_QUERY}")["data"]["search"]
    nodes = [n for n in data["nodes"] if n]
    team = mergers(nodes)

    def person(actor):
        return bool(actor) and actor.get("__typename") != "Bot" and not actor.get("login", "").lower().endswith("[bot]")

    def core(entry):
        actor = entry.get("author") or {}
        return person(actor) and (entry.get("authorAssociation") in CORE or actor.get("login", "").lower() in team)

    outside, answered, unknown = [], 0, 0
    for n in nodes:
        if not outsider(n, team) or parse_iso(n["createdAt"]) > last:
            continue
        deadline = parse_iso(n["createdAt"]) + dt.timedelta(days=days)
        author = n["author"]["login"].lower()
        comments, reviews = n.get("comments") or {}, n.get("reviews") or {}
        times = [n["mergedAt"]] if n.get("mergedAt") else []
        times += [c["createdAt"] for c in comments.get("nodes") or [] if c and core(c)]
        times += [r["submittedAt"] for r in reviews.get("nodes") or [] if r and r.get("submittedAt") and core(r)]
        times += [e["createdAt"] for e in (n.get("timelineItems") or {}).get("nodes") or []
                  if e and person(e.get("actor")) and e["actor"].get("login", "").lower() != author]
        if any(parse_iso(t) <= deadline for t in times):
            answered += 1
        elif truncated(comments) or truncated(reviews):
            unknown += 1
            continue
        outside.append(n)
    window = f"{first_text}..{last_text}"
    authors = len({n["author"]["login"].lower() for n in outside})
    enough = len(outside) >= least and authors >= people
    # An unknown PR may well have been answered, so it counts as answered for a rejection.
    unresponsive = enough and answered + unknown < share * (len(outside) + unknown)
    if not enough:
        reason = (f"{len(outside)} outside PRs from {authors} authors opened {window}, short of {least} PRs from {people} "
                  "authors: not enough history to reject the repository for unresponsiveness")
    else:
        reason = (f"{answered} of {len(outside)} outside PRs opened {window} got a maintainer response within {days} days"
                  + (f", under {share:.0%}: unresponsive" if unresponsive else ""))
    return records.save(repo, "responsiveness", {
        "repo": repo, "at": iso(at), "settings": settings, "window": window, "response_days": days,
        "prs_in_window": data["issueCount"],
        "sampled_prs": len(nodes), "outside_prs": len(outside), "outside_authors": authors, "answered": answered,
        "unknown_prs": unknown,
        "response_share": round(answered / len(outside), 2) if outside else None,
        "enough_history": enough, "unresponsive": unresponsive, "reason": reason,
    }, settings=settings)
