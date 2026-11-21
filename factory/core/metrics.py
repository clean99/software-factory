"""Read saved evidence, publish aggregates, and keep missing telemetry explicit.

No network reads, clone access, record updates, or interpretation of private prose.
Weekly acceptance uses submission cohorts; output uses event dates.
"""
import datetime as dt
import json
import math
import os
import pathlib
import re
import statistics
import subprocess
from collections import Counter, defaultdict
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from factory.core import leaks, privacy, repos, verdicts
from factory.util import CONFIG, ROOT, CommandError, load_config, public_destinations, write_json

MATURITY_DAYS = 14
MIN_RATE_SAMPLE = 20
ACTIVITY_ZONE = "Asia/Singapore"
ACTIVITY_DAYS = 28
SCHEMA_VERSION = 4
UTC = dt.timezone.utc
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/[1-9]\d*/?", re.ASCII)
OPEN = {"open", "submitted", "in-review", "community-review", "changes-requested", "ci-failing",
        "awaiting-cla", "silent"}
GROUPS = ("Outcome", "Output", "Review history", "Defects", "Filtering", "Factory speed", "Efficiency", "Factory health")


def timestamp(value, exact=False):
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return None if exact else dt.datetime.combine(dt.date.fromisoformat(value), dt.time(), UTC)
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except ValueError:
        return None


def number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def pr_url(value):
    if not isinstance(value, str) or not PR_URL.fullmatch(value):
        return None
    repository, number = value.rstrip("/").lower().removeprefix("https://github.com/").split("/pull/")
    return f"https://github.com/{repos.canonical(repository).lower()}/pull/{number}"


def github_link(value, api=True):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        hosts = {"github.com", "api.github.com"} if api else {"github.com"}
        return parsed.scheme == "https" and parsed.netloc in hosts and parsed.path.startswith("/")
    except ValueError:
        return False


def wilson(yes, total):
    if not total:
        return None
    z = 1.959963984540054
    share = yes / total
    scale = 1 + z * z / total
    centre = (share + z * z / (2 * total)) / scale
    margin = z * math.sqrt(share * (1 - share) / total + z * z / (4 * total * total)) / scale
    return [round(max(0, centre - margin), 4), round(min(1, centre + margin), 4)]


def improvements(data):
    try:
        if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1:
            raise ValueError()
        entries = data["entries"]
        if not isinstance(entries, list) or len(entries) > 20:
            raise ValueError()
        result = []
        for entry in entries:
            if not all(isinstance(entry[key], str) for key in ("date", "happened", "change", "moves")):
                raise ValueError()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["date"]) or not timestamp(entry["date"]):
                raise ValueError()
            if not isinstance(entry["links"], list):
                raise ValueError()
            links = []
            for link in entry["links"]:
                if not isinstance(link, dict) or not isinstance(link["label"], str) or ("url" in link) == ("path" in link):
                    raise ValueError()
                if "url" in link:
                    if not github_link(link["url"], api=False):
                        raise ValueError()
                    links.append({"label": link["label"], "url": link["url"]})
                else:
                    path = link["path"]
                    if (not isinstance(path, str) or not path or pathlib.PurePosixPath(path).is_absolute()
                            or '..' in path or any(c in path for c in ('\\', ':', '?', '#'))
                            or any(c.isspace() for c in path) or pathlib.PurePosixPath(path).as_posix() != path):
                        raise ValueError()
                    links.append({"label": link["label"], "path": path})
            result.append({**{key: entry[key] for key in ("date", "happened", "change", "moves")}, "links": links})
        return result, 0
    except (KeyError, TypeError, ValueError):
        return [], 1


def elapsed(start, end):
    a, b = timestamp(start, exact=True), timestamp(end, exact=True)
    return (b - a).total_seconds() / 3600 if a and b and b >= a else None


def week(value):
    date = timestamp(value)
    return (date.date() - dt.timedelta(days=date.weekday())).isoformat() if date else None


class Reader:
    def __init__(self):
        self.issues = Counter()

    def json(self, path, expected=dict):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, expected):
                raise ValueError("unexpected shape")
            return value
        except FileNotFoundError:
            self.issues["missing_files"] += 1
        except (OSError, ValueError, UnicodeError):
            self.issues["unreadable_files"] += 1
        return None

    def jsonl(self, path):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            self.issues["missing_files"] += 1
            return []
        except (OSError, UnicodeError):
            self.issues["unreadable_files"] += 1
            return []
        rows = []
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("unexpected shape")
                rows.append(row)
            except ValueError:
                self.issues["invalid_rows"] += 1
        return rows

    def reviews(self, folder, schema):
        # Rendered Markdown and JSON of the same numbered round are one review.
        structured = isinstance(schema, int) and schema >= 2
        paths = {}
        for path in sorted(folder.glob("review*")):
            # Match the leading round number, including suffixed archives, as review._round_records does.
            match = re.fullmatch(r"review(?:-round(\d+).*)?\.(json|md)", path.name)
            if not match or not path.is_file():
                continue
            key = int(match[1]) if match[1] else 0
            preferred = ".json" if structured else ".md"
            if key not in paths or path.suffix == preferred:
                paths[key] = path
        rows = []
        for path in paths.values():
            if path.suffix == ".json":
                row = self.json(path) or {}
            else:
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    self.issues["unreadable_files"] += 1
                    continue
                verdict, head = verdicts.VERDICT.search(text), verdicts.REVIEWED_SHA.search(text)
                row = {"verdict": verdict[1].lower() if verdict else None, "head": head[1] if head else None}
            if row.get("verdict") in verdicts.VERDICTS and row.get("complete", True) is True:
                head = row.get("head")
                head = head.lower() if isinstance(head, str) and re.fullmatch(r"[a-fA-F0-9]{7,40}", head) else None
                rows.append({"verdict": row["verdict"], "head": head})
        # A just-archived current review is not a second round.
        seen, result = set(), []
        for row in rows:
            key = (row["head"], row["verdict"])
            if row["head"] and key in seen:
                continue
            seen.add(key)
            result.append(row)
        return result


def history(root):
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "log", "--no-merges", "--format=%s", "--",
             "factory", "bin", "schedule", "hooks", "tests"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"},
        )
        if result.returncode:
            return None
        subjects = result.stdout.splitlines()
        return {"commits": len(subjects), "fix_commits": sum(bool(re.match(r"fix(?:es|ed)?\b", s, re.I)) for s in subjects)}
    except (OSError, subprocess.TimeoutExpired):
        return None


def item_paths(runs):
    """Walk dated candidate groups, stopping before an item's own artifacts."""
    root = runs.resolve()
    seen = set()
    for day in sorted(runs.glob("????-??-??")):
        if not day.is_dir() or day.is_symlink() or timestamp(day.name) is None:
            continue
        for folder, directories, files in os.walk(day):
            directories[:] = sorted(name for name in directories
                                    if not name.startswith(".") and name not in ("artifacts", "fixtures", "receipts"))
            if "item.json" not in files:
                continue
            directories[:] = []
            path = pathlib.Path(folder) / "item.json"
            resolved = path.resolve()
            if resolved.is_relative_to(root) and resolved not in seen:
                seen.add(resolved)
                yield path


def read_snapshot(root, config, suite_report=None, history_cache=None):
    root = pathlib.Path(root)
    reader = Reader()

    def configured(key, default):
        value = pathlib.Path(config.get(key, default)).expanduser()
        return value if value.is_absolute() else root / value

    ledger = configured("ledger_file", "ledger/prs.json")
    prs = reader.json(ledger, list)
    if prs is None:
        raise CommandError("metrics: the PR ledger must be a readable JSON array; no output written")
    runs = configured("runs_dir", "runs")
    items = []
    for path in item_paths(runs):
        meta = reader.json(path)
        if meta is None or meta.get("line", "oss") != "oss":
            continue
        folder = path.parent
        receipts = [row for p in sorted((folder / "receipts").glob("*.json"))
                    if (row := reader.json(p)) is not None]
        items.append({"meta": meta, "key": folder.relative_to(runs).as_posix(),
                      "qualify": reader.json(folder / "qualify.json"),
                      "value": reader.json(folder / "value-review.json"),
                      "reviews": reader.reviews(folder, meta.get("schema", 1)), "receipts": receipts})
    shift_dir = configured("repos_dir", "workspace/repos").parent / "shift-logs"
    shifts = [row for path in sorted(shift_dir.glob("*/summary.json"))
              if (row := reader.json(path)) is not None]
    try:
        owner_text = (root / "docs/owner-actions.md").read_text(encoding="utf-8")
        owner_rows = len(re.findall(r"^\|\s*\d{4}-\d{2}-\d{2}\s*\|", owner_text, re.M))
    except (OSError, UnicodeError):
        owner_rows = 0
        reader.issues["missing_owner_actions"] += 1
    events = reader.jsonl(ledger.with_name("followup-events.jsonl"))
    owner_ops = reader.jsonl(ledger.with_name("owner-ops.jsonl"))
    suite = reader.json(pathlib.Path(suite_report)) if suite_report else None
    pr_history = reader.json(pathlib.Path(history_cache) if history_cache else ledger.with_name("pr-history.json"))
    outcomes_path = ledger.with_name("pr-outcomes.json")
    outcomes = reader.json(outcomes_path) if outcomes_path.exists() else {"schema": 1, "closures": {}, "maintainer_edits": {}}
    public_improvements = reader.json(pathlib.Path(__file__).with_name("public-improvements.json"))
    return {"prs": prs, "items": items, "shifts": shifts, "events": events, "owner_ops": owner_ops,
            "outcomes": outcomes, "improvements": public_improvements,
            "pr_history": pr_history, "owner_action_rows": owner_rows, "suite": suite, "history": history(root),
            "input_quality": dict(reader.issues)}


def normalize_prs(rows):
    by_url, invalid, duplicates = {}, 0, 0
    rows = [r if isinstance(r, dict) else {} for r in rows]
    for row in sorted(rows, key=lambda r: timestamp(r.get("last_check")) or dt.datetime.min.replace(tzinfo=UTC)):
        url = pr_url(row.get("url"))
        if not url:
            invalid += 1
            continue
        duplicates += url in by_url
        by_url[url] = {**by_url.get(url, {}), **row, "url": url}
    for row in by_url.values():
        status = str(row.get("status") or row.get("state") or "").lower()
        row["outcome"] = status if status in ("merged", "closed") else None
        if status in OPEN:
            row["outcome"] = "open"
    return list(by_url.values()), invalid, duplicates


def suite_measurement(suite):
    if not isinstance(suite, dict) or not isinstance(suite.get("success"), bool):
        return None
    modules = suite.get("modules")
    if (not isinstance(modules, list) or not modules or not number(suite.get("tests"))
            or not number(suite.get("wall_seconds")) or not isinstance(suite["tests"], int)):
        return None
    if any(not isinstance(r, dict) or not isinstance(r.get("tests"), int) or not number(r["tests"])
           or not number(r.get("seconds")) or type(r.get("exit_code")) is not int for r in modules):
        return None
    if (sum(r["tests"] for r in modules) != suite["tests"]
            or suite["success"] != all(r["exit_code"] == 0 for r in modules)):
        return None
    return {"tests": suite["tests"], "seconds": suite["wall_seconds"], "passed": suite["success"]}


def public_links(config):
    try:
        repository, dashboard = public_destinations(config)
    except ValueError as error:
        raise CommandError(f"metrics: {error}") from None
    base = "https://github.com/" + repository
    return {"repository": base, "readme": base + "#readme", "direction": base + "/blob/HEAD/docs/direction.md",
            "dashboard": dashboard}


def cohort_projection(prs, outcomes, opened, generated_at):
    cutoff = generated_at.date() - dt.timedelta(days=MATURITY_DAYS)
    cohort = {"maturity_days": MATURITY_DAYS, "min_sample": MIN_RATE_SAMPLE, "as_of": generated_at.date().isoformat(), "cutoff": cutoff.isoformat(),
              "n": 0, "first_opened": None, "last_opened": None, "merged": 0, "merged_no_rework": 0,
              "merged_reworked": 0, "merged_unverified": 0, "maintainer_edits_neutral": 0,
              "closed": 0, "open": 0, "excluded_recent": 0, "excluded_undated": 0}
    reasons = {key: defaultdict(list) for key in ("merged_reworked", "closed", "open")}
    reasons["closed"]["unclassified"] = []
    repositories, pull_requests, dates, names = {}, [], [], {}
    for row in prs:
        name = repos.canonical(row.get("repo"))
        repository = row["url"].removeprefix("https://github.com/").split("/pull/")[0]
        if isinstance(name, str) and repos.NAME.fullmatch(name) and name.lower() == repository:
            names.setdefault(repository, name)
    for row in prs:
        url = row["url"]
        result, at = outcomes[url], opened[url]
        repository, number = url.removeprefix("https://github.com/").split("/pull/")
        repository = names.get(repository, repository)
        eligible = at is not None and at.date() <= cutoff
        pull_requests.append({"url": url, "repository": repository, "number": int(number),
                              "opened": at.date().isoformat() if at else None, "in_cohort": eligible,
                              **{key: result[key] for key in ("bucket", "reason", "flags", "evidence", "edit_class")}})
        if not eligible:
            cohort["excluded_recent" if at else "excluded_undated"] += 1
            continue
        dates.append(at.date().isoformat())
        cohort["n"] += 1
        bucket = result["bucket"]
        if bucket in cohort:
            cohort[bucket] += 1
        cohort["merged"] += row["outcome"] == "merged"
        cohort["maintainer_edits_neutral"] += row["outcome"] == "merged" and result["edit_class"] not in (None, "repair", "unclassified")
        if bucket in reasons:
            reasons[bucket][result["reason"]].append(url)
        counts = repositories.setdefault(repository, {"repository": repository, "opened": 0, "merged": 0, "merged_no_rework": 0, "closed": 0, "open": 0})
        counts["opened"] += 1
        counts["merged"] += row["outcome"] == "merged"
        for key in ("merged_no_rework", "closed", "open"):
            counts[key] += bucket == key
    if dates:
        cohort.update(first_opened=min(dates), last_opened=max(dates))
    by_repository = sorted((r for r in repositories.values() if r["opened"] >= 5), key=lambda r: (-r["opened"], r["repository"].lower()))
    other = [r for r in repositories.values() if r["opened"] < 5]
    if other:
        by_repository.append({"repository": "other", "repositories": len(other),
                              **{key: sum(r[key] for r in other) for key in ("opened", "merged", "merged_no_rework", "closed", "open")}})
    return {"cohort": cohort, "by_repository": by_repository,
            "pull_requests": sorted(pull_requests, key=lambda r: (r["opened"] or "", r["url"])),
            "reasons": {bucket: [{"key": key, "n": len(urls), "pull_requests": sorted(urls)}
                                 for key, urls in sorted(rows.items(), key=lambda pair: (-len(pair[1]), pair[0]))]
                        for bucket, rows in reasons.items()}}


def activity_rows(observation):
    """Validate saved activity before any lossy normalization can hide damage."""
    rows = observation.get("prs")
    if not isinstance(rows, list):
        return [], {"invalid_rows": 1, "duplicate_rows": 0, "population_difference": 0}
    prs, invalid, duplicates = normalize_prs(rows)
    for row in prs:
        events = row.get("events", [])
        if (row["outcome"] not in {"merged", "closed", "open"}
                or any(row.get(key) is not None and not timestamp(row[key], exact=True)
                       for key in ("created_at", "merged_at", "closed_at", "submitted_at", "updated_at"))
                or not isinstance(events, list) or any(not isinstance(event, dict)
                    or event.get("event") not in {"closed", "merged", "reopened"}
                    or not timestamp(event.get("created_at"), exact=True) for event in events)):
            invalid += 1
    difference = 0
    if "ledger_urls" in observation:
        urls = observation["ledger_urls"]
        if not isinstance(urls, list) or any(not pr_url(url) for url in urls):
            difference = 1
        else:
            expected = {pr_url(url) for url in urls}
            difference = len(expected ^ {r["url"] for r in prs}) + len(urls) - len(expected)
    return prs, {"invalid_rows": invalid, "duplicate_rows": duplicates, "population_difference": difference}


def activity_projection(rows, now, *, history=None, observation=None, replacements=None, expected_urls=None, replacement_errors=0):
    """Count contributions by local event date, separately from UTC PR cohorts."""
    from factory.core.metrics_history import cached_prs, normalize_outcomes, observation_error

    if now.tzinfo is None:
        raise ValueError("activity requires a timezone-aware snapshot time")
    zone = ZoneInfo(ACTIVITY_ZONE)
    observation = observation if isinstance(observation, dict) else {}
    checked = timestamp(observation.get("checked_at"), exact=True)
    quality = {"invalid_rows": 0, "duplicate_rows": 0, "population_difference": 0}
    saved_prs = []
    damaged = False
    if "prs" in observation or "checked_at" in observation:
        saved_prs, quality = activity_rows(observation)
        if expected_urls is not None and "ledger_urls" not in observation and not observation.get("error"):
            quality["population_difference"] = max(quality["population_difference"], len(set(expected_urls) ^ {r["url"] for r in saved_prs}))
        damaged = any(quality.values()) or checked is None
    saved = "prs" in observation and checked is not None and not damaged
    prs = saved_prs if saved else normalize_prs(rows)[0]
    cached = {} if saved else cached_prs(history)
    dates = []
    for row in prs:
        evidence = cached.get(row["url"], {})
        if evidence.get("status") == "available" and not evidence.get("refresh_error"):
            for field in ("state", "created_at", "merged_at", "closed_at", "events"):
                if field in evidence:
                    row[field] = evidence[field]
            row["outcome"] = evidence.get("state")
            if at := timestamp(evidence.get("fetched_at"), exact=True):
                dates.append(at)
        row["opened_at"] = timestamp(row.get("created_at"), exact=True) or timestamp(row.get("submitted_at"), exact=True)
    as_of = checked if saved else max(dates, default=None)
    as_of_time = as_of.isoformat(timespec="seconds") if as_of else None
    as_of_local_date = as_of.astimezone(zone).date().isoformat() if as_of else None
    local_date = (as_of or now).astimezone(zone).date()
    days = {(local_date - dt.timedelta(days=i)).isoformat(): {"opened": 0, "merged": 0, "closed_unmerged": 0}
            for i in reversed(range(ACTIVITY_DAYS))}
    by_url = {r["url"]: r for r in prs}
    pairs, invalid_pairs = normalize_outcomes({"schema": 2, "replacements": replacements if replacements is not None else []})
    invalid_pairs += replacement_errors
    old_urls, new_urls = set(), set()
    for pair in pairs["replacements"]:
        old, new = by_url.get(pair["old"]), by_url.get(pair["new"])
        closed = timestamp(old.get("closed_at"), exact=True) if old else None
        if (not old or not new or old["outcome"] != "closed" or not closed or not new["opened_at"]
                or closed >= new["opened_at"] or pair["old"] in old_urls or pair["new"] in new_urls):
            invalid_pairs += 1
            continue
        old_urls.add(pair["old"])
        new_urls.add(pair["new"])
    totals = {"opened": len(prs) - len(new_urls), "merged": 0, "closed_unmerged": 0, "open": 0,
              "first_opened_local_date": None, "projects_with_merges": 0}
    missing = {"opened_at": 0, "merged_at": 0, "closed_at": 0}
    opened_dates, merged_projects, reopened = [], set(), 0
    for row in prs:
        url, state = row["url"], row["outcome"]
        events = row.get("events") or []
        reopened += any(e.get("event") == "reopened" for e in events if isinstance(e, dict))
        activity = []
        if url not in new_urls:
            at = row["opened_at"]
            activity.append(("opened", "opened_at", at))
            if at:
                opened_dates.append(at.astimezone(zone).date().isoformat())
        if state == "merged":
            totals["merged"] += 1
            merged_projects.add(url.split("/pull/")[0])
            activity.append(("merged", "merged_at", timestamp(row.get("merged_at"), exact=True)))
        elif state == "closed" and url not in old_urls:
            totals["closed_unmerged"] += 1
            activity.append(("closed_unmerged", "closed_at", timestamp(row.get("closed_at"), exact=True)))
        elif state == "open":
            totals["open"] += 1
        for event, field, at in activity:
            if at is None:
                missing[field] += 1
            elif (date := at.astimezone(zone).date().isoformat()) in days:
                days[date][event] += 1
    totals.update(first_opened_local_date=min(opened_dates, default=None), projects_with_merges=len(merged_projects))
    decided = totals["merged"] + totals["closed_unmerged"] + totals["open"]
    difference = totals["opened"] - decided
    error = observation_error(observation.get("error")) if observation.get("error") else None
    if damaged:
        error = "invalid_response"
    return {"zone": ACTIVITY_ZONE, "as_of": as_of_time, "as_of_local_date": as_of_local_date,
            "observation": {"ok": saved and error is None, "checked_at": as_of_time,
                            "error": error or (None if saved else "not_checked")},
            "today": {"date": local_date.isoformat(), **days[local_date.isoformat()], "partial": True},
            "days": [{"date": date, **counts} for date, counts in days.items()], "window_days": ACTIVITY_DAYS,
            "totals": totals, "missing": missing, "replacements": {"pairs": len(old_urls)}, "reopened": reopened,
            "reconciliation": {"ok": difference == 0 and invalid_pairs == 0 and not damaged, "opened": totals["opened"], "outcomes": decided,
                               "difference": difference, "unclassified": sum(r["outcome"] not in {"merged", "closed", "open"} for r in prs),
                               "invalid_replacements": invalid_pairs, **quality}}


def compute(snapshot, config, generated_at=None):
    from factory.core.metrics_history import cached_prs, normalize_outcomes, observations, outcome, reconciliation

    generated_at = (generated_at or dt.datetime.now(UTC)).astimezone(UTC)
    prs, invalid, duplicates = normalize_prs(snapshot.get("prs", []))
    upstream = observations(prs, snapshot.get("pr_history"))
    cached = cached_prs(snapshot.get("pr_history"))
    classifications, invalid_outcomes = normalize_outcomes(snapshot.get("outcomes", {"schema": 1}))
    outcomes = {r["url"]: outcome(upstream.get(r["url"]), r, classifications) for r in prs}
    opened = {r["url"]: timestamp(cached.get(r["url"], {}).get("created_at"), exact=True)
              or timestamp(r.get("submitted_at"), exact=True) for r in prs}
    projection = cohort_projection(prs, outcomes, opened, generated_at)
    cohort = projection["cohort"]
    public_improvements, invalid_improvements = improvements(snapshot.get("improvements", {"schema": 1, "entries": []}))
    items = snapshot.get("items", [])
    shifts = snapshot.get("shifts", [])
    events = snapshot.get("events", [])
    merged = [r for r in prs if r["outcome"] == "merged"]
    concluded = [r for r in prs if r["outcome"] in ("merged", "closed")]
    known = [r for r in prs if r["outcome"]]
    metrics = {}

    def metric(key, label, group, value, unit, definition, source, covered, total, *, reason=None, note=None, partial=False):
        status = "partial" if partial or covered < total else "tracked"
        if value is None:
            status = "not_tracked"
        metrics[key] = {"label": label, "group": group, "value": round(value, 3) if isinstance(value, float) else value,
                        "unit": unit, "status": status, "definition": definition, "source": source,
                        "coverage": {"covered": covered, "total": total}, "reason": reason, "note": note}

    def ratio(key, label, group, yes, total, population, definition, source, note=None, partial=False):
        metric(key, label, group, yes / total if total else None, "percent", definition, source, total, population,
               reason="No eligible observations with the required fields." if not total else None, note=note, partial=partial)
        metrics[key].update(numerator=yes, denominator=total)

    ledger = "PR ledger: saved outcomes"
    metric("prs_opened", "PRs opened", "Output", len(prs), "count", "Distinct pull request URLs in the ledger, including all outcomes.",
           ledger, len(prs), len(prs) + invalid)
    for key, label, state in (("prs_merged", "PRs merged", "merged"), ("prs_open", "PRs open", "open"), ("prs_closed", "PRs closed", "closed")):
        value = sum(r["outcome"] == state for r in prs) if known or not prs else None
        metric(key, label, "Output", value, "count", "Distinct PRs with a saved " + state + " outcome.",
               ledger, len(known), len(prs), reason="No classified PR outcomes." if value is None else None,
               note="Saved state only; this command does not refresh GitHub.")
    open_rule = ("Open sub-states: awaiting-cla is agreement; otherwise no-reply requires silent status or explicit "
                 "responded.maintainer=false and responded.community=false, with neither a true human response flag "
                 "nor in-review/community-review status. A human response flag or in-review/community-review status is in-review; "
                 "other submissions are unchecked. "
                 "Source: factory/oss/followup.py:341-351,394-409; factory/oss/submit.py:336-339.")
    metrics["prs_open"]["definition"] += " " + open_rule
    ratio("cohort_merge_rate", "Cohort merge rate", "Outcome", cohort["merged"], cohort["n"], cohort["n"],
          f"Merged / all PRs opened on or before the date {MATURITY_DAYS} days before the snapshot date (UTC). Closed and unanswered PRs stay in the denominator; exact timestamps establish opening dates.", ledger,
          note="95% Wilson interval assumes independent PRs; repository clustering limits that assumption.")
    metrics["cohort_merge_rate"].update(interval=wilson(cohort["merged"], cohort["n"]), open=cohort["open"], closed=cohort["closed"])
    verified = cohort["merged"] - cohort["merged_unverified"]
    ratio("merged_no_rework_share", "Merged without rework", "Outcome", cohort["merged_no_rework"], verified, cohort["merged"],
          "Merged without rework: merged with no formal change request, no content change pushed by the author after opening, "
          "and no repair by a maintainer before merging / merged. Uses cohort merges with complete cached histories. "
          "Unclassified maintainer edits count against the share. Maintainer scope changes, refactors, style changes, tests, "
          "release notes, bot commits and base merges are recorded and do not count; merge-from-base commits are excluded.",
          "Saved role-only PR history; validated outcome classifications",
          note="Incomplete merged histories are excluded from this denominator and reduce coverage. Commit dates proxy ordinary push times; force-push times are exact.")
    metrics["merged_no_rework_share"]["unclassified_edits"] = sum(outcomes[r["url"]]["unclassified_edit"] for r in projection["pull_requests"] if r["in_cohort"])
    for key in ("cohort_merge_rate", "merged_no_rework_share"):
        if metrics[key]["denominator"] < MIN_RATE_SAMPLE:
            metrics[key].update(value=None, status="not_tracked", reason=f"Fewer than {MIN_RATE_SAMPLE} eligible PRs; counts are shown instead.")
    ratio("decided_merge_rate", "Merged among decided", "Outcome", len(merged), len(concluded), len(known),
          "Merged / (merged + closed without merge) over all saved PRs, not only the mature cohort.", ledger,
          note="Leaves out pull requests that are still open, so on the same data it reads higher than the cohort merge rate.")
    source = "GitHub PR metadata, paginated reviews, issue timeline and commits: saved role-only history cache"
    history_note = ("Coverage is complete cached histories / ledger PRs; unavailable histories are excluded, not clean. "
                    "Tracked requires at least 90% coverage. Committer dates proxy ordinary pushes, not actual push times or batches; "
                    "force-push times are exact. Merge commits are excluded. GitHub cannot recover every removed commit or delayed push.")

    def upstream_metric(key, label, group, value, unit, definition):
        metric(key, label, group, value, unit, definition, source, len(upstream), len(prs), note=history_note,
               reason="No complete cached histories or eligible observations. Run factory metrics history to backfill." if value is None else None)
        if value is not None:
            metrics[key]["status"] = "tracked" if len(upstream) >= .9 * len(prs) else "partial"

    for key, label, field in (("fixes_pushed_after_review", "PRs with fixes pushed after review", "after_review_pushes"),
                              ("fixes_pushed_before_review", "Pre-review self-corrections (defect signal)", "before_review_pushes")):
        upstream_metric(key, label, "Review history", sum(r[field] > 0 for r in upstream.values()) if upstream else None, "count",
                        "PRs with a self non-merge commit or force-push after submission "
                        + ("and after the first non-self review." if field == "after_review_pushes" else "before any non-self review, including PRs not yet reviewed.")
                        + " Total updates counts distinct commits and force-push events, not network push batches or proven code defects.")
        metrics[key]["total_pushes"] = sum(r[field] for r in upstream.values()) if upstream else None
    fix_hours = [hours for r in upstream.values() for hours in r["fix_hours"]]
    upstream_metric("review_fix_hours", "Review request to pushed fix", "Review history",
                    statistics.median(fix_hours) if fix_hours else None, "hours",
                    "Median hours from each non-self CHANGES_REQUESTED review to the next self non-merge commit or force-push. "
                    "Comment-only reviews are too ambiguous to establish a fix request; requests without a subsequent fix are excluded.")
    metrics["review_fix_hours"].update(samples=len(fix_hours), requests=sum(r["changes_requested"] for r in upstream.values()))
    by_item = {row["key"]: row for row in items}
    by_pr = {pr_url(row["meta"].get("pr_url")): row for row in items if pr_url(row["meta"].get("pr_url"))}

    def item_for(row):
        if row["url"] in by_pr:
            return by_pr[row["url"]]
        reference = row.get("item")
        if isinstance(reference, str):
            # Only match already-loaded records. Never open a path taken from the ledger.
            key = reference.replace("\\", "/").split("runs/", 1)[-1].rstrip("/")
            return by_item.get(key)
        return None

    lead = {}
    paired_items = [(row, item_for(row)) for row in prs]
    for row, item in paired_items:
        value = elapsed(item["meta"].get("created_at"), row.get("submitted_at")) if item else None
        if value is not None:
            lead[row["url"]] = value
    metric("median_lead_hours", "Median candidate-to-PR lead time", "Factory speed", statistics.median(lead.values()) if lead else None, "hours",
           "Median hours from the persisted candidate item's creation to PR submission; scouting before item creation is not recorded. Date-only, naive and reversed timestamps are excluded.",
           "Item metadata: created_at; PR ledger: submitted_at", len(lead), len(prs),
           reason="No nonnegative pair of timezone-aware timestamps." if not lead else None)
    reviewed_prs = [item for _, item in paired_items if item and item.get("reviews")]
    metric("candidate_selection_misses", "Candidate-selection misses", "Defects", None, "count",
           "PRs closed without merge because they were duplicate, already fixed or unwanted; each PR counts once.",
           "PR ledger: closed outcomes; follow-up closure events", 0, len(concluded) - len(merged),
           reason="Closure reasons are reported separately; no dedicated candidate-selection-miss classification is recorded. Private notes and generic closed events are not interpreted.")
    failed = set()
    observed = set()
    for row in prs:
        checks = row.get("checks")
        if isinstance(checks, dict) and isinstance(checks.get("failing"), list) and number(checks.get("total")) and checks["total"] > 0:
            observed.add(row["url"])
            if checks["failing"]:
                failed.add(row["url"])
    urls = {r["url"] for r in prs}
    for event in events:
        url = pr_url(event.get("url"))
        if url in urls and event.get("kind") == "ci-failing":
            failed.add(url)
            observed.add(url)
    metric("ci_failures_after_submission", "PRs with observed CI failures", "Defects", len(failed) if observed else None, "count",
           "Distinct submitted PRs with a failing CI snapshot or a ci-failing follow-up event. Repeated observations count once; this is not a code-caused bug rate.",
           "PR ledger: checks; follow-up events: ci-failing", len(observed), len(prs), partial=True,
           reason="No saved CI observations." if not observed else None,
           note="Observed minimum only. Successful latest checks do not prove that earlier runs passed; failures on the base or infrastructure are not attributed to the PR.")
    for key, label in (("post_merge_reverts", "Post-merge reverts"), ("post_merge_regressions", "Post-merge regressions")):
        metric(key, label, "Defects", None, "count", "Merged PRs with a confirmed subsequent " + ("revert." if key.endswith("reverts") else "regression."),
               "Follow-up events; PR ledger", 0, len(merged),
               reason="Follow-up stops observing merged PRs; no post-merge incident registry exists. Red/green test reverts are not production reverts.")
    rounds = [len(item["reviews"]) for item in reviewed_prs]
    metric("independent_review_rounds_per_pr", "Pre-publication independent review rounds per PR", "Review history", statistics.mean(rounds) if rounds else None, "rounds",
           "Mean preserved independent review verdicts per PR with review records; current and numbered archives are deduplicated by head and verdict.",
           "Item review files and numbered review archives", len(rounds), len(prs), partial=True,
           reason="No readable independent review verdicts." if not rounds else None,
           note="Pre-publication independent reviews, not upstream maintainer rounds. Missing or overwritten archives undercount rounds.")
    metric("candidates_found", "Recorded candidates", "Filtering", len(items), "count", "OSS item records persisted under the runs directory; pre-item scouting ideas are not counted.",
           "Item metadata", len(items), len(items), partial=True, note="This is the persisted candidate population, not all opportunities inspected.")
    published_items = {i["key"] for _, i in paired_items if i}
    stopped = sum(i["meta"].get("status") in {"dropped", "parked", "blocked", "abandoned"}
                  and not i["meta"].get("pr_url") and i["key"] not in published_items for i in items)
    metric("candidates_stopped_before_publish", "Candidates stopped before publishing", "Filtering", stopped, "count",
           "Item records with status dropped, parked, blocked or abandoned, no PR URL and no matching PR in the ledger.",
           "Item metadata: status, pr_url; PR ledger item references", len(items), len(items))
    qualified = [i["qualify"] for i in items if isinstance(i.get("qualify"), dict) and i["qualify"].get("verdict") in ("ok", "skip")]
    values = [i["value"] for i in items if isinstance(i.get("value"), dict) and i["value"].get("verdict") in verdicts.VALUE_VERDICTS
              and i["value"].get("complete") is True]
    reviewed = [i for i in items if i.get("reviews")]
    for key, label, observations, count, source, definition in (
        ("dropped_qualify", "Rejected at qualification", qualified, sum(q["verdict"] == "skip" for q in qualified), "Item qualify records",
         "Candidates with a saved skip verdict. Earlier rejections overwritten by a later check are not recoverable."),
        ("dropped_value", "Dropped at value review", values, sum(v["verdict"] == "drop" for v in values), "Item value-review records",
         "Candidates with a complete structured value review whose verdict is drop. Legacy prose is not classified."),
        ("sent_back_review", "Sent back by independent review", reviewed, sum(any(r["verdict"] == "fix-needed" for r in i["reviews"]) for i in reviewed),
         "Item review files and numbered review archives", "Candidates with at least one preserved fix-needed independent review verdict; each candidate counts once."),
    ):
        metric(key, label, "Filtering", count if observations else None, "count", definition, source, len(observations), len(items), partial=True,
               reason="No readable structured verdicts for this stage." if not observations else None)
    for kind, label, population in (("pr", "PR", prs), ("merge", "merged PR", merged)):
        for prefix, work, unit in (("agent_minutes", "Agent minutes", "minutes"), ("tool_calls", "Tool calls", "calls")):
            metric(f"{prefix}_per_{kind}", f"{work} per {label}", "Efficiency", None, unit,
                   f"Total attributable {work.lower()} / {label}s in the same work cohort.",
                   "Shift summaries; item command receipts", 0, len(population),
                   reason="Shift telemetry excludes nested workers and has no PR attribution. Command runtime is not agent time; dividing partial recent shifts by lifetime PR outcomes would be misleading.")
    metric("unattended_shifts", "Shifts run unattended", "Efficiency", None, "count", "Completed shifts with no manual intervention from start to finish.",
           "Shift summaries; owner actions", 0, len(shifts),
           reason="Automated launch and exit status do not establish absence of human help. Shifts have no intervention flag or linked intervention log.")
    metric("owner_interventions", "Owner interventions per week", "Efficiency", None, "count", "Manual interventions actually performed, grouped by UTC week.",
           "Owner action notes; delegated account-operation records", 0, snapshot.get("owner_action_rows", 0) + len(snapshot.get("owner_ops", [])),
           reason="Owner action notes mix requests, resolutions and delegated work; account-operation logs record agent actions, not manual interventions. They cannot be counted as human actions.")
    for key, label, field, unit in (("coordinator_minutes", "Recorded coordinator minutes", "active_minutes", "minutes"),
                                    ("coordinator_tool_calls", "Recorded coordinator tool calls", "tool_calls", "calls")):
        values = [s[field] for s in shifts if number(s.get(field))]
        metric(key, label, "Efficiency", sum(values) if values else None, unit, "Sum of " + field + " in saved automated-shift summaries; not full-factory cost.",
               "Shift summaries", len(values), len(shifts), partial=True, reason="No valid shift telemetry." if not values else None,
               note="Coordinator only. Worker sessions are excluded by the shift summarizer; telemetry covers only retained shift logs.")
    statuses = [s["status"] for s in shifts if isinstance(s.get("status"), str) and s["status"].strip()]
    ratio("shift_completion_rate", "Shift completion rate", "Factory health", statuses.count("exited 0"), len(statuses), len(shifts),
          "Automated shift summaries with status exactly exited 0 / summaries with a recorded terminal status. Failed starts count against completion.", "Shift summaries: status")
    shift_dates = [at.date().isoformat() for s in shifts if (at := timestamp(s.get("ended_at"), exact=True))]
    metrics["shift_completion_rate"]["since"] = min(shift_dates) if shift_dates else None
    suite = suite_measurement(snapshot.get("suite"))
    suite_note = None
    if suite:
        suite_note = "Recorded suite passed." if suite["passed"] else "Recorded suite FAILED; counts are not a green build."
    for key, label, field, unit in (("factory_test_count", "Factory test count", "tests", "count"),
                                    ("factory_test_runtime", "Factory test runtime", "seconds", "seconds")):
        metric(key, label, "Factory health", suite[field] if suite else None, unit,
               "Test count or wall-clock runtime from the explicitly supplied full-suite runner report; never inferred from source or unrelated item tests.",
               "Full-suite runner JSON report supplied with --suite-report", int(suite is not None), 1,
               reason="No valid full-suite report supplied. Capture tests/run_suite.py --json and pass that file with --suite-report." if not suite else None,
               note=suite_note)
    for key, label in (("factory_defects_found", "Factory defects found"), ("factory_defects_fixed", "Factory defects fixed")):
        metric(key, label, "Factory health", None, "count", "Distinct confirmed factory defects " + ("found." if key.endswith("found") else "fixed."),
               "Factory git history; no defect registry", 0, 0,
               reason="There is no stable defect identifier linking discovery and resolution. A repair commit may fix several defects or revisit one; commit counts are shown separately.")
    history_data = snapshot.get("history")
    metric("factory_fix_commits", "Factory repair commits", "Factory health", history_data["fix_commits"] if history_data else None, "count",
           "Non-merge commits touching factory, bin, schedule, hooks or tests whose subject begins fix, fixes or fixed. Counts commits, not distinct defects.",
           "Local git history", history_data["commits"] if history_data else 0, history_data["commits"] if history_data else 0,
           reason="Git history could not be read." if history_data is None else None,
           note="Only explicitly labeled repair commits; differently worded repairs are excluded.")
    weekly = weekly_series(prs, lead, upstream, outcomes, opened)
    history_cache = snapshot.get("pr_history")
    history_cache = history_cache if isinstance(history_cache, dict) else {}
    check = history_cache.get("reconciliation")
    check = check if isinstance(check, dict) else {}
    raw_outcomes = snapshot.get("outcomes")
    pairs, invalid_pairs = normalize_outcomes({"schema": 2, "replacements": raw_outcomes.get("replacements", []) if isinstance(raw_outcomes, dict) else []})
    activity = activity_projection(prs, generated_at, history=history_cache, observation=check.get("activity"),
                                   replacements=pairs["replacements"], replacement_errors=invalid_pairs,
                                   expected_urls={r["url"] for r in prs})
    return {"schema_version": SCHEMA_VERSION, "generated_at": generated_at.isoformat(timespec="seconds"), "activity": activity,
            "links": public_links(config), "metrics": metrics, "weekly": weekly, **projection,
            "reconciliation": reconciliation(snapshot.get("pr_history")), "improvements": public_improvements,
            "input_quality": {**snapshot.get("input_quality", {}), "invalid_pr_rows": invalid, "duplicate_pr_rows": duplicates,
                              "invalid_outcome_rows": invalid_outcomes, "invalid_improvements": invalid_improvements},
            "population": {"prs": len(prs), "items": len(items), "shift_summaries": len(shifts),
                           "projects": len({r["repository"].lower() for r in projection["pull_requests"]}),
                           "merged_projects": len({r["repository"].lower() for r in projection["pull_requests"] if r["bucket"].startswith("merged_")}),
                           "command_receipts": sum(len(i.get("receipts", [])) for i in items)},
            "methodology": ["Read-only snapshot of retained local evidence. No network read or GitHub refresh is performed.",
                            f"The cohort includes PRs opened on or before the date {MATURITY_DAYS} days before the snapshot date (UTC), using exact cached creation timestamps or ledger submission timestamps to establish opening dates. Time of day does not affect membership; undated PRs are excluded and counted.",
                            "Drafts, closed, withdrawn and unanswered PRs remain in the cohort merge-rate denominator. Later merges move open rows to merged without changing cohort membership.",
                            f"Rates need at least {MIN_RATE_SAMPLE} eligible PRs. The 95% Wilson interval assumes independent PRs; the repository split shows their clustering. Weekly outcomes are counts, not trend claims.",
                            "Merged-without-rework uses only cohort merges with complete histories. Missing histories reduce coverage, never count as clean; unclassified maintainer edits count against the share.",
                            "Committer dates proxy ordinary pushes, not exact push times or batches. Force-push times are exact. Merge commits are excluded; removed commits or delayed pushes may be missed.",
                            "History stores self/other roles, not human/bot identity. Open response reasons use the saved ledger's human response flags. " + open_rule,
                            "Daily activity uses Asia/Singapore event dates, not the UTC cohort dates. Openings use creation times; merges and unmerged closures use their own terminal times. Reopened PRs count only their current terminal outcome. Recorded replacement pairs are one daily contribution but remain separate PRs in the cohort.",
                            "Daily activity carries the last successful GitHub observation time. A failed refresh retains its counts and date window; missing event times remain outside daily rows but inside totals. Today is partial, and waiting time is not a KPI.",
                            "All weeks start Monday in UTC. Weekly outcomes reflect the current saved state of every PR opened that week, including recent PRs. Open counts are not historical week-end inventory.",
                            "Coverage is eligible records with needed fields / the stated population, not proof of complete historical capture. Missing values are never zero; input read errors and omitted dates can undercount.",
                            "Factory speed measures candidate-to-submission work, not waiting for maintainers. Maintainer response, approval and merge latency are not measured."]}


def weekly_series(prs, lead, upstream, outcomes, opened):
    buckets = defaultdict(lambda: {"opened": 0, "merged": 0, "cohort_merged": 0, "cohort_no_rework": 0,
                                   "cohort_closed": 0, "cohort_open": 0, "lead_hours": [], "fix_hours": [],
                                   "history_covered": 0, "history_total": 0})
    submitted_coverage = merged_coverage = 0
    for row in prs:
        url = row["url"]
        evidence = upstream.get(url)
        at = opened[url]
        opened_week = week(at.isoformat()) if at else None
        if opened_week:
            submitted_coverage += 1
            bucket = buckets[opened_week]
            bucket["opened"] += 1
            bucket["cohort_merged"] += row["outcome"] == "merged"
            bucket["cohort_no_rework"] += outcomes[url]["bucket"] == "merged_no_rework"
            bucket["cohort_closed"] += row["outcome"] == "closed"
            bucket["cohort_open"] += row["outcome"] == "open"
            bucket["history_total"] += 1
            if evidence:
                bucket["history_covered"] += 1
                bucket["fix_hours"].extend(evidence["fix_hours"])
            if url in lead:
                bucket["lead_hours"].append(lead[url])
        if row["outcome"] == "merged" and (merged_week := week(row.get("merged_at"))):
            merged_coverage += 1
            buckets[merged_week]["merged"] += 1
    rows = []
    if buckets:
        current, end = dt.date.fromisoformat(min(buckets)), dt.date.fromisoformat(max(buckets))
        while current <= end:
            key = current.isoformat()
            bucket = buckets[key]
            lead_values = bucket.pop("lead_hours")
            fix_values = bucket.pop("fix_hours")
            rows.append({"week": key, **bucket,
                         "median_lead_hours": round(statistics.median(lead_values), 3) if lead_values else None,
                         "lead_time_samples": len(lead_values),
                         "review_fix_hours": round(statistics.median(fix_values), 3) if fix_values else None,
                         "review_fix_samples": len(fix_values), "owner_interventions": None})
            current += dt.timedelta(days=7)
    return {"rows": rows, "coverage": {"submission_dates": {"covered": submitted_coverage, "total": len(prs)},
                                      "merge_dates": {"covered": merged_coverage, "total": sum(r["outcome"] == "merged" for r in prs)}}}


def scan_output(text):
    if privacy.findings("metrics output", text, prose=True) or leaks.scan(text=text, label="metrics output"):
        raise CommandError("metrics: generated output did not pass the privacy and secret scans; no output written")


def public_files(root, *, config=None, records_root=None, suite_report=None):
    """Project saved records into the same scanned static assets used by the CLI."""
    from factory.core.metrics_site import figures, render, render_board

    records = pathlib.Path(records_root or root).resolve()
    snapshot = read_snapshot(records, load_config(records), suite_report)
    result = compute(snapshot, CONFIG if config is None else config)
    files = {"site/metrics.json": json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
             "site/index.html": render(result, config=config),
             "site/board.html": render_board(result, config=config),
             **{'site/' + name: text for name, text in figures(result).items()}}
    for text in files.values():
        scan_output(text)
    return files


def command(args):
    from factory.core.metrics_site import figures, render, render_board, status_card

    root = pathlib.Path(args.records_root).resolve() if args.records_root else ROOT
    try:
        config = load_config(root)
        snapshot = read_snapshot(root, config, args.suite_report, args.history_cache)
        # Public surface configuration belongs to the running factory, not the historical records being read.
        result = compute(snapshot, CONFIG)
        if getattr(args, "metrics_command", None) == "card":
            card = status_card(result)
            scan_output(card)
            print(card, end="")
            return 0
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        files = {"index.html": render(result), "board.html": render_board(result), **figures(result)} if args.site else {}
        scan_output(text)
        for contents in files.values():
            scan_output(contents)
        output = pathlib.Path(args.output)
        site = pathlib.Path(args.site) if args.site else None
        if site and any(output.resolve() == (site / name).resolve() for name in files):
            raise CommandError("metrics: the JSON output and site assets must be different files")
        output.parent.mkdir(parents=True, exist_ok=True)
        write_json(output, result)
        if site:
            site.mkdir(parents=True, exist_ok=True)
            for name, contents in files.items():
                (site / name).write_text(contents, encoding="utf-8")
    except (OSError, ValueError, TypeError):
        raise CommandError("metrics: unreadable configuration, records or output destination") from None
    print(f"{'Metric':<40}{'Value':<22}Coverage")
    for key in ("cohort_merge_rate", "merged_no_rework_share", "prs_opened", "prs_merged", "prs_open", "prs_closed", "median_lead_hours", "shift_completion_rate"):
        row = result["metrics"][key]
        value = row["value"]
        if value is None:
            text = "not tracked"
        elif row["unit"] == "percent":
            text = f"{value:.1%}"
        else:
            text = f"{value:g}" + (" h" if row["unit"] == "hours" else "")
        coverage = row["coverage"]
        print(f"{row['label']:<40}{text:<22}{coverage['covered']}/{coverage['total']}")
    print(f"{sum(m['status'] == 'not_tracked' for m in result['metrics'].values())} metrics not tracked; definitions and reasons are in the JSON.")
    return 0


def register(sub):
    import argparse
    from factory.core.metrics_history import register as register_history

    parser = sub.add_parser("metrics", help="read saved evidence and export privacy-scanned portfolio metrics (no network)")
    inputs = (("--records-root", "read another factory checkout and its configured records without updating them"),
              ("--history-cache", "read a separate PR history cache offline (default: beside the PR ledger)"),
              ("--suite-report", "full-suite JSON produced by tests/run_suite.py --json; no tests are run by metrics"))
    for option, help_text in inputs:
        parser.add_argument(option, help=help_text)
    parser.add_argument("--output", "-o", default="metrics/metrics.json", help="JSON output path (default: metrics/metrics.json)")
    parser.add_argument("--site", help="also write self-contained index.html, board.html and all SVG figures to this directory")
    parser.set_defaults(fn=command)
    register_history(parser)
    actions = parser._subparsers._group_actions[0]
    card = actions.add_parser("card", help="print the scanned text status card from saved evidence (no network or file writes)")
    for option, help_text in inputs:
        card.add_argument(option, default=argparse.SUPPRESS, help=help_text)
    card.set_defaults(fn=command)
