"""Is this repo a good place to spend effort?

Three numbers decide most of it: how many outsiders actually get merged, how fast, and how
flooded the repo is with outsider PRs that die unmerged (the AI-slop pressure on maintainers).
"""
import datetime as dt

from factory.util import gh_json, iso, parse_iso

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
