"""Where a shift starts: which items can move now, which repos have room, and how our merged
work ranks in the repos that took it.

`run` reads only local files and clones. `influence` makes read-only GitHub calls and, with
record=True, appends one line to ledger/influence.jsonl. Anything that cannot be checked is
reported with the reason, never counted as passing.
"""
import json
import statistics

from factory.core import clones, gate, ledger, review as protocol, verdicts
from factory.core.repos import canonical
from factory.util import CONFIG, ROOT, CommandError, GH_READ_TIMEOUT, Item, gh_json, iso, now, parse_iso, path_from_config, read_json

PARKED = {"parked", "blocked"}
INACTIVE = PARKED | {"dropped", "closed", "abandoned", "merged"}
CONTRIBUTOR_PAGES = 5
PAGE_SIZE = 100


def rel(path):
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def ledger_rows():
    try:
        rows = ledger.load()
    except (OSError, ValueError) as error:
        raise SystemExit(f"ledger {path_from_config('ledger_file')} is unreadable ({error}); repair it before planning") from None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise SystemExit(f"ledger {path_from_config('ledger_file')} is not a list of PR rows; repair it before planning")
    return rows


def validate(item, part):
    try:
        return getattr(protocol, f"validate_{part}")(item)
    except (OSError, ValueError) as error:
        raise verdicts.EvidenceError(f"{protocol.PARTS[part]}: {error}; repair the evidence and re-review") from None


def classify(item):
    """(bucket, entry) for one item; unreadable evidence does not stop the other items."""
    entry = {"item": rel(item.dir), "repo": item.meta.get("repo"), "status": item.meta.get("status")}
    try:
        return _classify(item, entry)
    except verdicts.EvidenceError as error:
        return "errors", {**entry, "error": str(error)}


def _classify(item, entry):
    meta = item.meta
    if item.schema >= 2:
        problems = validate(item, "demand")
        if problems:
            return "held", {**entry, "reasons": problems}
        if read_json(item.file("demand.json"))["decision"] == "drop":
            return "held", {**entry, "reasons": ["the scout dropped this candidate: set status to dropped in item.json with the reason"]}
        if not item.file("value-review.json").exists():
            return "needs_value_review", entry
        problems = validate(item, "value")
        if problems:
            return "held", {**entry, "reasons": problems}
        value = read_json(item.file("value-review.json"))
        if type(value.get("complete", True)) is not bool:
            return "held", {**entry, "reasons": ["Add value-review.complete as true or false."]}
        if value.get("complete") is False:
            return "held", {**entry, "reasons": ["Finish value-review.json and set complete to true before continuing."]}
        if value["verdict"] == "drop":
            return "held", {**entry, "reasons": ["the value review says drop: set status to dropped in item.json and copy its reason"]}
        if value["verdict"] == "propose":
            if (verdicts.markdown_text(item.file("proposal.md")) or "").strip():
                return "proposal_ready", {**entry, "verdict": "propose"}
            return "held", {**entry, "reasons": ["write proposal.md as the value-review card describes"]}
        if not item.file("verify.md").exists():
            return "needs_implementation", entry
        if not meta.get("pr_url"):
            self_review = verdicts.read_self_review(item) or {}
            if self_review.get("verdict") != "ship":
                return "needs_implementation", entry
    else:
        try:
            review = verdicts.read_review(item)
        except (OSError, UnicodeError) as error:
            raise verdicts.EvidenceError(f"review.md cannot be read ({error}); restore readable Markdown and re-review") from None
    try:
        clone = item.clone
    except KeyError:
        return "errors", {**entry, "error": "item.json names neither clone nor repo"}
    if not clone.is_dir() and clones.released(item) is None:
        return "missing_clone", {**entry, "clone": str(clone)}
    if item.schema >= 2:
        try:
            if item.head_sha() == item.merge_base():
                return "needs_implementation", entry
        except (CommandError, OSError, KeyError) as error:
            return "errors", {**entry, "error": f"cannot compare branch with base: {error}; restore the item branch and base ref"}
        review = verdicts.read_review(item)
        verdicts.read_coordinator(item)
    if review is None:
        return "unreviewed", entry
    try:
        head = item.head_sha()
    except (CommandError, KeyError) as error:
        return "errors", {**entry, "error": f"branch head unknown: {error}"}
    reviewed = review["head"]
    entry.update(verdict=review["verdict"], reviewed=reviewed, head=head)
    if item.schema >= 2:
        problems = validate(item, "review")
        moved = (bool(reviewed) and not verdicts.bound(item, head, reviewed) or
                 bool(review["pr_sha256"]) and review["pr_sha256"] != verdicts.pr_text_sha256(item))
        if moved:
            return "stale_reviews", {**entry, "reasons": problems}
        reviewer, implementer = gate.independence_names(item, review["reviewer"])
        independence, why = gate.review_independence(reviewer, implementer, meta.get("kind"))
        if independence == "fail":
            problems.append(why + "; get an independent review")
        if problems:
            return "held", {**entry, "reasons": problems}
    if not reviewed:
        field = "head field" if item.schema >= 2 else "head: line"
        target = rel(item.dir)
        repair = (f"if it reviewed the current head, bind it with bin/factory review stamp --item {target} review --head <head>; "
                  f"if it reviewed an earlier head, archive it with bin/factory review round --item {target} and let next request a fresh review")
        return "held", {**entry, "reasons": [f"{review['source']} has no {field}, so it is bound to no commit; {repair}"]}
    if not verdicts.bound(item, head, reviewed):
        return "stale_reviews", entry
    if entry["verdict"] == "fix-needed":
        suffix = "json" if item.schema >= 2 else "md"
        return "fix_round", {**entry, "review": rel(item.file(review["source"])),
                             "review_rounds": len(list(item.dir.glob(f"review-round*.{suffix}")))}
    reasons = [] if entry["verdict"] == "ship" else [f"review verdict is {entry['verdict'] or 'missing'}"]
    self_review = (verdicts.read_self_review(item) or {}).get("verdict")
    if self_review != "ship":
        reasons.append(f"verify.md verdict is {self_review or 'missing'}")
    if item.schema >= 2:
        reasons.extend(validate(item, "demand"))
    else:
        missing = gate.user_impact_missing(item.file("verify.md"))
        if missing:
            reasons.append(f"verify.md lacks {' and '.join(missing)}")
    if reasons:
        return "held", {**entry, "reasons": reasons}
    if item.schema >= 2:
        problems = validate(item, "coordinator")
        if problems:
            return "needs_coordinator_review", {**entry, "reasons": problems}
    rep = gate.Report()
    gate.coordinator_check(item, rep, head)
    if not rep.ok:
        return "needs_coordinator_review", {**entry, "reasons": [r["detail"] for r in rep.rows if not r["ok"]]}
    return "ship_ready", entry


def slot(rows):
    limits = CONFIG["limits"]
    merged = [r for r in rows if r.get("status") == "merged"]
    open_rows = [r for r in rows if r.get("status") not in ledger.DONE]
    cap = limits["max_open_prs_per_repo"] if merged else limits["max_open_prs_per_repo_before_first_merge"]
    hours = [(parse_iso(r["merged_at"]) - parse_iso(r["submitted_at"])).total_seconds() / 3600
             for r in merged if r.get("merged_at") and r.get("submitted_at")]
    last_merge = max((parse_iso(r["merged_at"]) for r in merged if r.get("merged_at")), default=None)
    stale_days = limits.get("stale_open_days", 3)
    reasons = [f"#{r.get('number')} is silent" for r in rows if r.get("status") == "silent"]
    ages = []
    for r in open_rows:
        if not r.get("submitted_at"):
            reasons.append(f"#{r.get('number')} is open with no submitted_at, so its age is unknown")
            continue
        submitted = parse_iso(r["submitted_at"])
        ages.append((now() - submitted).total_seconds() / 86400)
        if ages[-1] >= stale_days and (last_merge is None or last_merge < submitted):
            reasons.append(f"#{r.get('number')} open {ages[-1]:.1f} days with no merge since (stale_open_days {stale_days})")
    return {
        "repo": canonical(rows[0].get("repo")), "open": len(open_rows), "cap": cap, "free": max(cap - len(open_rows), 0),
        "merged": len(merged), "median_merge_hours": round(statistics.median(hours), 1) if hours else None,
        "oldest_open_days": round(max(ages), 1) if ages else None, "stopped": bool(reasons), "stop_reasons": reasons,
    }


def by_repo(rows):
    grouped = {}
    for r in rows:
        grouped.setdefault((canonical(r.get("repo")) or "").lower(), []).append(r)
    return grouped


def repo_slots(rows):
    """Repos that merged our work, those with a usable slot first, then by merges and merge speed."""
    slots = []
    for repo, group in by_repo(rows).items():
        if not any(r.get("status") == "merged" for r in group):
            continue
        try:
            slots.append(slot(group))
        except (ValueError, TypeError) as error:
            raise SystemExit(f"ledger rows for {repo} carry an unreadable date ({error}); repair them before planning") from None
    return sorted(slots, key=lambda s: (not (s["free"] and not s["stopped"]), -s["merged"],
                                        s["median_merge_hours"] is None, s["median_merge_hours"] or 0))


def _proposal_posted(item):
    """A proposal is out as a comment (proposal_url) or as the item's own issue, opened because the value review said propose."""
    meta = item.meta
    if meta.get("status") != "awaiting-maintainer":
        return False
    if meta.get("proposal_url"):
        return True
    if not meta.get("tracking_issue"):
        return False
    try:
        value = read_json(item.file("value-review.json"), {})
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("verdict") == "propose" and value.get("complete", True) is True


def run():
    report = {"at": iso(), "ship_ready": [], "needs_coordinator_review": [], "fix_round": [], "stale_reviews": [], "held": [], "unreviewed": [],
              "missing_clone": [], "errors": [], "parked_or_blocked": [], "repo_slots": repo_slots(ledger_rows()),
              "needs_value_review": [], "proposal_ready": [], "needs_implementation": []}
    for path in sorted(path_from_config("runs_dir").glob("*/*/item.json")):
        try:
            item = Item(path.parent)
            meta, status = item.meta, item.meta.get("status")
        except (OSError, ValueError, AttributeError) as error:
            report["errors"].append({"item": rel(path.parent), "error": f"item.json unreadable: {error}"})
            continue
        if status in PARKED:
            reason = meta.get("blocked_reason") or meta.get("parked_reason") or ""
            report["parked_or_blocked"].append({"item": rel(item.dir), "repo": meta.get("repo"), "status": status, "reason": reason[:160]})
        elif not meta.get("pr_url") and status not in INACTIVE:
            if item.schema >= 2 and _proposal_posted(item):
                continue
            bucket, entry = classify(item)
            report[bucket].append(entry)
    return report


def contributor_rank(repo, user):
    """1-based position of user among the first CONTRIBUTOR_PAGES pages of contributors, and how many were seen."""
    rank, seen = None, 0
    for page in range(1, CONTRIBUTOR_PAGES + 1):
        batch = gh_json("api", f"repos/{repo}/contributors?per_page={PAGE_SIZE}&page={page}", timeout=GH_READ_TIMEOUT) or []
        if not isinstance(batch, list):
            raise ValueError(f"contributors page {page} is not a list")
        for person in batch:
            seen += 1
            if rank is None and str(person.get("login") or "").lower() == user.lower():
                rank = seen
        if len(batch) < PAGE_SIZE:
            break
    return rank, seen


def repo_reading(repo, merged, user):
    try:
        stars = gh_json("api", f"repos/{repo}", "--jq", ".stargazers_count", timeout=GH_READ_TIMEOUT)
        if type(stars) is not int:
            raise ValueError(f"stargazers_count is {stars!r}")
        rank, seen = contributor_rank(repo, user)
    except (CommandError, ValueError) as error:
        return {"repo": repo, "merged": merged, "error": str(error)[:300]}
    return {"repo": repo, "merged": merged, "stars": stars, "rank": rank, "contributors_seen": seen}


def history_path():
    return path_from_config("ledger_file").with_name("influence.jsonl")


def last_reading(path):
    lines = [line for line in (path.read_text().splitlines() if path.exists() else []) if line.strip()]
    if not lines:
        return None
    try:
        previous = json.loads(lines[-1])
    except ValueError:
        previous = None
    if not isinstance(previous, dict) or not isinstance(previous.get("repos"), list):
        raise SystemExit(f"the last line of {path} is not an influence reading; repair it so deltas compare against real data")
    return previous


def previous_by_repo(entries):
    """Earlier readings by current name; one taken before a rename was mapped can list the repo twice."""
    before = {}
    for r in entries:
        if not isinstance(r, dict):
            continue
        key = canonical(str(r.get("repo"))).lower()
        old = before.get(key)
        if old:
            rank = old.get("rank") if type(old.get("rank")) is int else r.get("rank")
            r = {**old, "merged": (old.get("merged") or 0) + (r.get("merged") or 0), "rank": rank}
        before[key] = r
    return before


def with_delta(reading, previous):
    before = previous_by_repo(previous["repos"])
    repos = []
    for r in reading["repos"]:
        old = before.get(r["repo"].lower())
        if old is None:
            delta = {"new": True, "merged": r["merged"], "rank": None, "previous_rank": None}
        else:
            ranks = (r.get("rank"), old.get("rank"))
            delta = {"merged": r["merged"] - (old.get("merged") or 0), "previous_rank": ranks[1],
                     "rank": ranks[0] - ranks[1] if all(type(x) is int for x in ranks) else None}
        repos.append({**r, "delta": delta})
    followers = (reading.get("followers"), previous.get("followers"))
    return {**reading, "repos": repos, "previous_at": previous.get("at"),
            "followers_delta": followers[0] - followers[1] if all(type(x) is int for x in followers) else None}


def influence(record=False):
    """Where our merged work stands. Rank deltas are new minus old, so a negative rank delta means we climbed."""
    user = CONFIG["github_user"]
    path = history_path()
    previous = last_reading(path)
    groups = by_repo(ledger_rows())
    merged = {canonical(group[0]["repo"]): sum(r.get("status") == "merged" for r in group) for key, group in groups.items() if key}
    repos = sorted((repo_reading(repo, count, user) for repo, count in merged.items() if count),
                   key=lambda r: ("error" in r, -r.get("stars", 0), r["repo"].lower()))
    reading = {"at": iso(), "user": user, "followers": None, "public_repos": None,
               "merged_total": sum(r["merged"] for r in repos), "repos": repos}
    try:
        profile = gh_json("api", f"users/{user}", timeout=GH_READ_TIMEOUT) or {}
        reading.update(followers=profile["followers"], public_repos=profile["public_repos"])
    except (CommandError, ValueError, KeyError, TypeError) as error:
        reading["profile_error"] = str(error)[:300]
    report = with_delta(reading, previous) if previous else {**reading, "previous_at": None}
    report["recorded"] = None
    if record:
        refused = ("profile lookup failed" if "profile_error" in reading
                   else "every repo lookup failed" if repos and all("error" in r for r in repos) else None)
        if refused:
            report["record_refused"] = f"{refused}; nothing appended to {rel(path)}"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                handle.write(json.dumps(reading, ensure_ascii=False) + "\n")
            report["recorded"] = rel(path)
    return report
