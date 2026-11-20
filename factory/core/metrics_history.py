"""Read-only GitHub history acquisition and offline first-round classification.

Only role labels and structural evidence survive projection. Committer timestamps
are proxies for pushes: GitHub's timeline does not expose ordinary push batches.
"""
import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
from collections import Counter

from factory.core.metrics import normalize_prs, timestamp
from factory.util import CONFIG, ROOT, CommandError, write_json

SCHEMA_VERSION = 1
REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}
EVENTS = {"head_ref_force_pushed", "review_requested", "closed", "merged"}
SHA = re.compile(r"[0-9a-f]{40}")


class HistoryError(Exception):
    """An intentionally content-free error suitable for the private cache."""


def api(endpoint, *, paginate=False):
    command = [str(ROOT / "bin/factory"), "gh", "api", endpoint, "--method", "GET"]
    if paginate:
        command += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise HistoryError("transport_error") from None
    if result.returncode:
        error = result.stderr.lower()
        if "404" in error or "410" in error:
            raise HistoryError("unavailable")
        if "rate limit" in error or "429" in error or "403" in error:
            raise HistoryError("rate_limited_or_forbidden")
        raise HistoryError("request_failed")
    try:
        data = json.loads(result.stdout)
        if paginate:
            if not isinstance(data, list) or any(not isinstance(page, list) for page in data):
                raise ValueError()
            data = [row for page in data for row in page]
        return data
    except ValueError:
        raise HistoryError("invalid_response") from None


def date(value):
    parsed = timestamp(value, exact=True)
    if parsed is None:
        raise HistoryError("incomplete_history")
    return parsed.isoformat(timespec="seconds")


def role(user, author):
    login = (user or {}).get("login")
    if not isinstance(login, str) or not login:
        raise HistoryError("incomplete_identity")
    return "self" if login.lower() == author else "other"


def commit_role(commit, author):
    # GitHub signs web-created commits as web-flow; the author is the human actor.
    committer = commit.get("committer") or {}
    if committer.get("login") and committer["login"].lower() != "web-flow":
        return role(committer, author)
    return role(commit.get("author"), author)


def project(pr, reviews, timeline, commits, resolve_commit):
    """Project API responses, never retaining logins, email addresses or prose."""
    author = pr["user"]["login"].lower()
    created = date(pr["created_at"])
    merged = date(pr["merged_at"]) if pr.get("merged_at") else None
    if pr.get("state") not in {"open", "closed"}:
        raise HistoryError("invalid_response")
    result = {"status": "available", "created_at": created, "merged_at": merged,
              "closed_at": date(pr["closed_at"]) if pr.get("closed_at") else None,
              "state": "merged" if merged else pr["state"], "reviews": [], "commits": [], "events": []}
    # A dismissed change request still happened. Recover its previous state from
    # the dismissal event rather than treating the current DISMISSED state as clean.
    dismissed = {e.get("dismissed_review", {}).get("review_id"): e.get("dismissed_review", {}).get("state", "").upper()
                 for e in timeline if e.get("event") == "review_dismissed"}
    by_review = {r["id"]: r for r in timeline if r.get("event") == "reviewed"}
    by_review.update({r["id"]: r for r in reviews})
    for review in by_review.values():
        state = review["state"].upper()
        if state == "PENDING":
            continue
        if state == "DISMISSED":
            state = dismissed.get(review["id"])
        if state not in REVIEW_STATES:
            raise HistoryError("incomplete_history")
        result["reviews"].append({"state": state, "submitted_at": date(review["submitted_at"]),
                                  "role": role(review.get("user"), author)})
    by_sha = {c["sha"]: c for c in commits}
    timeline_commits = {e["sha"]: e for e in timeline if e.get("event") == "committed"}
    if len(timeline_commits.keys() | by_sha.keys()) < pr.get("commits", 0):
        raise HistoryError("incomplete_history")
    for sha in dict.fromkeys([*timeline_commits, *by_sha]):
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise HistoryError("invalid_response")
        full = by_sha.get(sha) or resolve_commit(sha)
        details = full["commit"]
        parents = full.get("parents")
        if not isinstance(parents, list) or not parents:
            raise HistoryError("incomplete_history")
        result["commits"].append({"sha": sha, "author_at": date(details["author"]["date"]),
                                  "committed_at": date(details["committer"]["date"]),
                                  "merge": len(parents) > 1, "role": commit_role(full, author)})
    for event in timeline:
        if event.get("event") in EVENTS:
            result["events"].append({"event": event["event"], "created_at": date(event["created_at"]),
                                     "role": role(event.get("actor"), author)})
    for key, time_key in (("reviews", "submitted_at"), ("commits", "committed_at"), ("events", "created_at")):
        result[key].sort(key=lambda row: row[time_key])
    return result


def fetch(url):
    repository, number = url.removeprefix("https://github.com/").split("/pull/")
    base = f"repos/{repository}"
    pr = api(f"{base}/pulls/{number}")
    reviews = api(f"{base}/pulls/{number}/reviews?per_page=100", paginate=True)
    timeline = api(f"{base}/issues/{number}/timeline?per_page=100", paginate=True)
    commits = api(f"{base}/pulls/{number}/commits?per_page=100", paginate=True)
    # The pull commits endpoint has a documented 250-commit ceiling. Timeline
    # commits not in it are resolved individually, including force-pushed heads.
    try:
        return project(pr, reviews, timeline, commits, lambda sha: api(f"{base}/commits/{sha}"))
    except (KeyError, TypeError, AttributeError):
        raise HistoryError("invalid_response") from None


def load_cache(path):
    try:
        cache = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": SCHEMA_VERSION, "prs": {}}
    except (OSError, ValueError, UnicodeError):
        raise CommandError("metrics history: unreadable cache; no cache overwritten") from None
    if not isinstance(cache, dict) or cache.get("schema_version") != SCHEMA_VERSION or not isinstance(cache.get("prs"), dict):
        raise CommandError("metrics history: unsupported cache; no cache overwritten")
    return cache


def retain_observed(previous, current):
    """Do not erase a previously observed fix when a rebase removes its commit."""
    if not isinstance(previous, dict) or previous.get("status") != "available":
        return current
    keys = {"commits": ("sha",), "reviews": ("submitted_at", "state", "role"),
            "events": ("created_at", "event", "role")}
    for field, identity in keys.items():
        combined = {tuple(row[key] for key in identity): row for row in previous[field] + current[field]}
        before = Counter(tuple(row[key] for key in identity) for row in previous[field])
        after = Counter(tuple(row[key] for key in identity) for row in current[field])
        # Two reviewers can submit the same state in the same second. Retain
        # multiplicity without retaining their identities or counting refetches.
        current[field] = [combined[key] for key, count in (before | after).items() for _ in range(count)]
    return current


def refresh(rows, cache_path, *, force=False, limit=None):
    prs, _, _ = normalize_prs(rows)
    cache = load_cache(cache_path)
    pathlib.Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
    urls = {r["url"] for r in prs}
    cache["prs"] = {url: row for url, row in cache["prs"].items() if url in urls}
    pending = [r["url"] for r in prs if force or not (
        cache["prs"].get(r["url"], {}).get("status") == "available"
        and cache["prs"][r["url"]].get("state") in {"merged", "closed"}
        and not cache["prs"][r["url"]].get("refresh_error"))]
    pending.sort(key=lambda url: cache["prs"].get(url, {}).get("fetched_at", ""))
    summary = {"refreshed": 0, "unavailable": 0, "skipped": len(prs) - len(pending), "pending": len(pending), "stopped": None}
    for url in pending[:limit]:
        fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        try:
            row = retain_observed(cache["prs"].get(url), fetch(url))
        except HistoryError as error:
            reason = str(error)
            if reason in {"unavailable", "incomplete_identity", "incomplete_history"}:
                row = {"status": "unavailable", "reason": reason}
                summary["unavailable"] += 1
            else:
                # Keep earlier evidence on a failed refresh, but exclude it from
                # metrics until refreshed; an old clean snapshot is not proof.
                row = {**cache["prs"].get(url, {"status": "unavailable"}), "refresh_error": reason}
                summary["stopped"] = reason
        row["fetched_at"] = fetched_at
        cache["prs"][url] = row
        write_json(cache_path, cache)
        if summary["stopped"]:
            break
        summary["refreshed"] += 1
        summary["pending"] -= 1
    return summary


def classify(row):
    """Return only complete classifications; missing evidence never means clean."""
    if not isinstance(row, dict) or row.get("status") != "available" or row.get("refresh_error"):
        return None
    try:
        created = timestamp(row["created_at"], exact=True)
        merged = timestamp(row.get("merged_at"), exact=True)
        if not created or row["state"] not in {"merged", "closed", "open"} or (row["state"] == "merged" and not merged):
            return None
        end = merged or timestamp(row.get("closed_at"), exact=True)
        reviews = []
        for r in row["reviews"]:
            at = timestamp(r["submitted_at"], exact=True)
            if not at or r["role"] not in {"self", "other"} or r["state"] not in REVIEW_STATES:
                return None
            if r["role"] == "other" and at >= created and (not end or at <= end):
                reviews.append((at, r["state"]))
        reviews.sort()
        self_pushes, other_pushes = [], []
        seen = set()
        for c in row["commits"]:
            at = timestamp(c["committed_at"], exact=True)
            if (not at or not timestamp(c["author_at"], exact=True) or c["role"] not in {"self", "other"}
                    or type(c["merge"]) is not bool or not SHA.fullmatch(c["sha"])):
                return None
            if c["sha"] in seen:
                continue
            seen.add(c["sha"])
            if not c["merge"] and at > created and (not end or at <= end):
                (self_pushes if c["role"] == "self" else other_pushes).append(at)
        forces = set()
        for e in row["events"]:
            at = timestamp(e["created_at"], exact=True)
            if not at or e["role"] not in {"self", "other"} or e["event"] not in EVENTS:
                return None
            if e["event"] == "head_ref_force_pushed" and at > created and (not end or at <= end):
                if e["role"] == "self":
                    forces.add(at)
                else:
                    other_pushes.append(at)
        # A force-push at the exact same timestamp as a commit is one observed
        # update, not two. Different commit timestamps cannot identify batches.
        self_pushes.extend(forces - set(self_pushes))
        self_pushes.sort()
        changes = [at for at, state in reviews if state == "CHANGES_REQUESTED"]
        first = reviews[0][0] if reviews else None
        after = [at for at in self_pushes if first and at > first]
        before = [at for at in self_pushes if not first or at < first]
        fix_hours = []
        for request in changes:
            next_push = next((at for at in self_pushes if at > request), None)
            if next_push:
                fix_hours.append((next_push - request).total_seconds() / 3600)
        reviewed = bool(merged or reviews)
        accepted = bool(merged or any(state == "APPROVED" for _, state in reviews))
        return {"created_at": row["created_at"], "reviewed": reviewed,
                "clean": reviewed and accepted and not changes and not self_pushes and not (merged and other_pushes),
                "changes_requested": len(changes), "rounds": len(changes) + 1 if reviewed else None,
                "self_pushes": len(self_pushes), "other_pushes": len(other_pushes),
                "after_review_pushes": len(after), "before_review_pushes": len(before), "fix_hours": fix_hours}
    except (KeyError, TypeError, AttributeError, ValueError):
        return None


def observations(prs, cache):
    rows = cache.get("prs", {}) if isinstance(cache, dict) and cache.get("schema_version") == SCHEMA_VERSION else {}
    return {r["url"]: result for r in prs if (result := classify(rows.get(r["url"]))) is not None}


def command(args):
    # A foreign records root contributes only ledger/prs.json, never its config,
    # hooks, credentials, or writable paths. The cache belongs to this checkout.
    if args.ledger:
        ledger = pathlib.Path(args.ledger)
    elif args.records_root:
        ledger = pathlib.Path(args.records_root) / "ledger/prs.json"
    else:
        ledger = pathlib.Path(CONFIG.get("ledger_file", "ledger/prs.json"))
        if not ledger.is_absolute():
            ledger = ROOT / ledger
    cache_path = pathlib.Path(args.cache).resolve()
    if not cache_path.is_relative_to((ROOT / "ledger").resolve()) or cache_path == ledger.resolve():
        raise CommandError("metrics history: cache must be a separate file under this checkout's ledger directory")
    try:
        rows = json.loads(ledger.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError()
        result = refresh(rows, cache_path, force=args.force, limit=args.limit)
    except (OSError, ValueError, UnicodeError):
        raise CommandError("metrics history: unreadable ledger or cache destination") from None
    print(json.dumps(result, sort_keys=True))
    return 3 if result["stopped"] else 0


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def register(parser):
    history = parser.add_subparsers(dest="metrics_command").add_parser("history", help="cache read-only GitHub evidence through factory gh")
    history.add_argument("--records-root", help="read only ledger/prs.json under another checkout")
    history.add_argument("--ledger", help="read this PR ledger instead")
    history.add_argument("--cache", default=str(ROOT / "ledger/pr-history.json"), help="cache under this checkout's ledger directory")
    history.add_argument("--limit", type=positive, help="maximum PRs to fetch this time (oldest snapshots first)")
    history.add_argument("--refresh-open", action="store_true", help="refresh open PRs (the default); concluded snapshots are reused")
    history.add_argument("--force", action="store_true", help="also refresh already cached concluded PRs")
    history.set_defaults(fn=command)
