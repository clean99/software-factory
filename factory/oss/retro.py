"""Phase retrospective numbers: where our merged work lands, how fast, and how heavy it is.

One GraphQL search over our PRs gives state, merge time, size and the target repo's stars. The
summary answers the questions a retro asks: merge rate on settled PRs, latency, size, which repos
carry the merges, which repos never answered, and how merges follow submissions day by day.
"""
import collections
import datetime as dt
import json
import pathlib

from factory.core.repos import canonical
from factory.util import CONFIG, gh_json, iso, parse_iso

QUERY = """query($q:String!,$cursor:String){search(query:$q,type:ISSUE,first:100,after:$cursor){pageInfo{hasNextPage endCursor}
nodes{... on PullRequest{url number title state createdAt mergedAt closedAt additions deletions
repository{nameWithOwner stargazerCount owner{login}}}}}}"""


OWNER_ZONE = dt.timezone(dt.timedelta(hours=8))


def fetch(user, since, pages=5, population="submitted"):
    nodes, cursor = [], None
    field = "merged" if population == "merged" else "created"
    q = f"author:{user} is:pr {field}:>={since} -user:{user}"
    for _ in range(pages):
        args = ["api", "graphql", "-f", f"q={q}", "-f", f"query={QUERY}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        data = gh_json(*args)["data"]["search"]
        nodes += [n for n in data["nodes"] if n]
        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]
    if data["pageInfo"]["hasNextPage"]:
        raise ValueError("retrospective search exceeds the page limit; narrow --since before reporting totals")
    return nodes


def quantile(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, int(len(values) * q))], 1) if values else None


def per_day(prs, field, zone):
    return dict(sorted(collections.Counter(parse_iso(p[field]).astimezone(zone).date().isoformat()
                                           for p in prs if p.get(field)).items()))


def summarize(prs, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    state = lambda p: "merged" if p.get("mergedAt") else p["state"].lower()
    hours = lambda p: (parse_iso(p["mergedAt"]) - parse_iso(p["createdAt"])).total_seconds() / 3600
    size = lambda p: (p.get("additions") or 0) + (p.get("deletions") or 0)
    merged = [p for p in prs if state(p) == "merged"]
    closed = [p for p in prs if state(p) == "closed"]
    opened = [p for p in prs if state(p) == "open"]

    repos = collections.defaultdict(lambda: {"stars": 0, "merged": 0, "open": 0, "closed": 0, "hours": [], "size": []})
    for p in prs:
        r = repos[canonical(p["repository"]["nameWithOwner"])]
        r["stars"] = p["repository"]["stargazerCount"]
        r[state(p)] += 1
        if state(p) == "merged":
            r["hours"].append(hours(p))
            r["size"].append(size(p))
    by_repo = sorted(
        ({"repo": name, "stars": r["stars"], "merged": r["merged"], "open": r["open"], "closed": r["closed"],
          "median_merge_hours": quantile(r["hours"], 0.5), "median_size": quantile(r["size"], 0.5)} for name, r in repos.items()),
        key=lambda r: (-r["merged"], -r["stars"]))

    tiers = collections.Counter()
    for p in merged:
        stars = p["repository"]["stargazerCount"]
        tiers[">=20k" if stars >= 20000 else "10k-20k" if stars >= 10000 else "<10k"] += 1
    silent_days = CONFIG["limits"]["silent_days"]
    return {
        "at": iso(now),
        "total": len(prs), "merged": len(merged), "closed": len(closed), "open": len(opened),
        "merge_rate_settled": round(len(merged) / (len(merged) + len(closed)), 2) if merged or closed else None,
        "merge_hours": {q: quantile([hours(p) for p in merged], v) for q, v in (("p25", 0.25), ("p50", 0.5), ("p75", 0.75), ("max", 1.0))},
        "merged_size": {"p50": quantile([size(p) for p in merged], 0.5), "max": quantile([size(p) for p in merged], 1.0)},
        "merged_by_star_tier": dict(tiers),
        "single_merge_big_repos": [r["repo"] for r in by_repo if r["merged"] == 1 and r["stars"] >= 20000],
        "no_merge_repos": [r["repo"] for r in by_repo if r["merged"] == 0 and r["closed"] == 0],
        "open_older_than_silent_days": sum(1 for p in opened if (now - parse_iso(p["createdAt"])).days >= silent_days),
        "day_timezone": "UTC+8",
        "submitted_per_day": per_day(prs, "createdAt", OWNER_ZONE),
        "merged_per_day": per_day(merged, "mergedAt", OWNER_ZONE),
        "utc": {"day_timezone": "UTC", "submitted_per_day": per_day(prs, "createdAt", dt.timezone.utc),
                "merged_per_day": per_day(merged, "mergedAt", dt.timezone.utc)},
        "closed_unmerged": [{"url": p["url"], "closed": (p.get("closedAt") or "")[:10], "title": p["title"]} for p in closed],
        "by_repo": by_repo,
    }


def since_date(since):
    try:
        return dt.datetime.combine(dt.date.fromisoformat(since), dt.time(), tzinfo=OWNER_ZONE)
    except ValueError:
        raise ValueError("--since must be a date in YYYY-MM-DD form (midnight UTC+8)") from None


def populations(prs, since, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    start = since_date(since)
    submitted = [p for p in prs if start <= parse_iso(p["createdAt"]) <= now]
    merged = [p for p in prs if p.get("mergedAt") and start <= parse_iso(p["mergedAt"]) <= now]
    return {"at": iso(now), "since": since, "day_timezone": "UTC+8", "since_utc": iso(start.astimezone(dt.timezone.utc)),
            "populations": {"submitted since": summarize(submitted, now), "merged since": summarize(merged, now)}}


def report(user, since, write=None):
    start = since_date(since)
    query_since = start.astimezone(dt.timezone.utc).date().isoformat()
    prs = {}
    for population in ("submitted", "merged"):
        for pr in fetch(user, query_since, population=population):
            prs[pr["url"]] = pr
    result = populations(list(prs.values()), since)
    if write is not None:
        directory = pathlib.Path(write)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "stats.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (directory / "prs.json").write_text(json.dumps(list(prs.values()), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def run(user, since):
    return summarize(fetch(user, since))
