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

from factory.core import leaks, privacy, verdicts
from factory.util import CONFIG, ROOT, CommandError, load_config, public_destinations, write_json

SCHEMA_VERSION = 3
UTC = dt.timezone.utc
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/[1-9]\d*/?", re.ASCII)
OPEN = {"open", "submitted", "in-review", "community-review", "changes-requested", "ci-failing",
        "awaiting-cla", "silent"}
GROUPS = ("Output", "Acceptance", "First-round quality", "Defects", "Filtering", "Factory speed", "Efficiency", "Factory health")


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
    return value.rstrip("/").lower() if isinstance(value, str) and PR_URL.fullmatch(value) else None


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
    return {"prs": prs, "items": items, "shifts": shifts, "events": events, "owner_ops": owner_ops,
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


def compute(snapshot, config, generated_at=None):
    prs, invalid, duplicates = normalize_prs(snapshot.get("prs", []))
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
    for key, label, state in (("prs_merged", "PRs merged", "merged"), ("prs_open", "PRs open", "open")):
        value = sum(r["outcome"] == state for r in prs) if known or not prs else None
        metric(key, label, "Output", value, "count", "Distinct PRs with a saved " + state + " outcome.",
               ledger, len(known), len(prs), reason="No classified PR outcomes." if value is None else None,
               note="Saved state only; this command does not refresh GitHub.")
    ratio("merge_rate", "Merge rate", "Acceptance", len(merged), len(concluded), len(known),
          "Merged / (merged + closed without merge). Open and unclassified outcomes are excluded.", ledger,
          note="Coverage counts concluded PRs against all classified PRs; open PRs are not failures.")
    ratio("closed_unmerged_share", "Closed without merge", "Acceptance", len(concluded) - len(merged), len(concluded), len(known),
          "Closed without merge / (merged + closed without merge).", ledger)
    from factory.core.metrics_history import observations
    upstream = observations(prs, snapshot.get("pr_history"))
    reviewed_history = [r for r in upstream.values() if r["reviewed"]]
    clean = sum(r["clean"] for r in reviewed_history)
    source = "GitHub PR metadata, paginated reviews, issue timeline and commits: saved role-only history cache"
    history_note = ("Coverage is complete cached histories / ledger PRs; unavailable histories are excluded, not clean. "
                    "Tracked requires at least 90% coverage. Committer dates proxy ordinary pushes, not actual push times or batches; "
                    "force-push times are exact. Merge commits are excluded. GitHub cannot recover every removed commit or delayed push.")

    def upstream_metric(key, label, group, value, unit, definition):
        metric(key, label, group, value, unit, definition, source, len(upstream), len(prs), note=history_note,
               reason="No complete cached histories or eligible observations. Run factory metrics history to backfill." if value is None else None)
        if value is not None:
            metrics[key]["status"] = "tracked" if len(upstream) >= .9 * len(prs) else "partial"

    upstream_metric("first_round_approval_share", "First-round approval", "First-round quality",
                    clean / len(reviewed_history) if reviewed_history else None, "percent",
                    "Approved or merged with no non-self change request, no self non-merge commit/force-push after submission, "
                    "and (if merged) no non-self corrective update before merge / reviewed PRs. "
                    "Reviewed means merged or at least one non-self APPROVED, CHANGES_REQUESTED or COMMENTED review; self reviews do not count.")
    metrics["first_round_approval_share"].update(numerator=clean, denominator=len(reviewed_history),
                                                excluded_histories=len(prs) - len(upstream),
                                                unreviewed=len(upstream) - len(reviewed_history))
    for key, label, field in (("fixes_pushed_after_review", "PRs with fixes pushed after review", "after_review_pushes"),
                              ("fixes_pushed_before_review", "Pre-review self-corrections (defect signal)", "before_review_pushes")):
        upstream_metric(key, label, "Defects", sum(r[field] > 0 for r in upstream.values()) if upstream else None, "count",
                        "PRs with a self non-merge commit or force-push after submission "
                        + ("and after the first non-self review." if field == "after_review_pushes" else "before any non-self review, including PRs not yet reviewed.")
                        + " Total updates counts distinct commits and force-push events, not network push batches or proven code defects.")
        metrics[key]["total_pushes"] = sum(r[field] for r in upstream.values()) if upstream else None
    fix_hours = [hours for r in upstream.values() for hours in r["fix_hours"]]
    upstream_metric("review_fix_hours", "Review request to pushed fix", "Factory speed",
                    statistics.median(fix_hours) if fix_hours else None, "hours",
                    "Median hours from each non-self CHANGES_REQUESTED review to the next self non-merge commit or force-push. "
                    "Comment-only reviews are too ambiguous to establish a fix request; requests without a subsequent fix are excluded.")
    metrics["review_fix_hours"].update(samples=len(fix_hours), requests=sum(r["changes_requested"] for r in upstream.values()))
    upstream_metric("review_rounds_per_pr", "Upstream review rounds per PR", "First-round quality",
                    statistics.mean(r["rounds"] for r in reviewed_history) if reviewed_history else None, "rounds",
                    "Mean non-self CHANGES_REQUESTED review count + 1 over reviewed PRs. Distinct from pre-publication independent review.")
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
           reason="Closed outcomes have no structured closure-reason classification. Private notes and generic closed events cannot reliably distinguish selection misses from other closures.")
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
    metric("independent_review_rounds_per_pr", "Pre-publication independent review rounds per PR", "First-round quality", statistics.mean(rounds) if rounds else None, "rounds",
           "Mean preserved independent review verdicts per PR with review records; current and numbered archives are deduplicated by head and verdict.",
           "Item review files and numbered review archives", len(rounds), len(prs), partial=True,
           reason="No readable independent review verdicts." if not rounds else None,
           note="Pre-publication independent reviews, not upstream maintainer rounds. Missing or overwritten archives undercount rounds.")
    metric("candidates_found", "Recorded candidates", "Filtering", len(items), "count", "OSS item records persisted under the runs directory; pre-item scouting ideas are not counted.",
           "Item metadata", len(items), len(items), partial=True, note="This is the persisted candidate population, not all opportunities inspected.")
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
    for cohort, label, population in (("pr", "PR", prs), ("merge", "merged PR", merged)):
        for prefix, work, unit in (("agent_minutes", "Agent minutes", "minutes"), ("tool_calls", "Tool calls", "calls")):
            metric(f"{prefix}_per_{cohort}", f"{work} per {label}", "Efficiency", None, unit,
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
    weekly = weekly_series(prs, lead, upstream)
    return {"schema_version": SCHEMA_VERSION, "generated_at": (generated_at or dt.datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds"),
            "links": public_links(config), "metrics": metrics, "weekly": weekly,
            "input_quality": {**snapshot.get("input_quality", {}), "invalid_pr_rows": invalid, "duplicate_pr_rows": duplicates},
            "population": {"prs": len(prs), "items": len(items), "shift_summaries": len(shifts),
                           "command_receipts": sum(len(i.get("receipts", [])) for i in items)},
            "methodology": ["Read-only snapshot of retained local evidence. No GitHub refresh is performed.",
                            "Coverage is eligible records with needed fields / the stated population, not proof of complete historical capture.",
                            "Partial means observed records only. Not tracked means the evidence cannot support the metric; missing values are never zero.",
                            "All weeks start Monday in UTC. Acceptance uses submission cohorts at their current saved outcome, not week-of-decision cohorts.",
                            "Weekly open counts are currently open PRs grouped by submission week, not historical week-end inventory.",
                            "Factory speed measures candidate-to-submission lead time and request-to-fix work, not waiting for maintainers. Response/merge latency and silent-thread share are intentionally excluded.",
                            "Zero event counts mean no events in retained records for that week. Input read errors and omitted dates can undercount."]}


def weekly_series(prs, lead, upstream=None):
    upstream = upstream or {}
    buckets = defaultdict(lambda: {"opened": 0, "merged": 0, "open_submission_cohort": 0,
                                   "cohort_merged": 0, "cohort_concluded": 0, "lead_hours": [], "fix_hours": [],
                                   "first_round_numerator": 0, "first_round_denominator": 0,
                                   "history_covered": 0, "history_total": 0})
    submitted_coverage = merged_coverage = 0
    for row in prs:
        evidence = upstream.get(row["url"])
        history_week = week(evidence["created_at"] if evidence else row.get("submitted_at"))
        if history_week:
            bucket = buckets[history_week]
            bucket["history_total"] += 1
            if evidence:
                bucket["history_covered"] += 1
                bucket["first_round_numerator"] += evidence["clean"]
                bucket["first_round_denominator"] += evidence["reviewed"]
                bucket["fix_hours"].extend(evidence["fix_hours"])
        submitted_week = week(row.get("submitted_at"))
        if submitted_week:
            submitted_coverage += 1
            bucket = buckets[submitted_week]
            bucket["opened"] += 1
            bucket["open_submission_cohort"] += row["outcome"] == "open"
            bucket["cohort_merged"] += row["outcome"] == "merged"
            bucket["cohort_concluded"] += row["outcome"] in ("merged", "closed")
            if row["url"] in lead:
                bucket["lead_hours"].append(lead[row["url"]])
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
            reviewed = bucket["first_round_denominator"]
            concluded = bucket["cohort_concluded"]
            rows.append({"week": key, **bucket,
                         "merge_rate": bucket["cohort_merged"] / concluded if concluded else None,
                         "closed_unmerged_share": (concluded - bucket["cohort_merged"]) / concluded if concluded else None,
                         "median_lead_hours": round(statistics.median(lead_values), 3) if lead_values else None,
                         "lead_time_samples": len(lead_values),
                         "review_fix_hours": round(statistics.median(fix_values), 3) if fix_values else None,
                         "review_fix_samples": len(fix_values),
                         "first_round_approval_share": bucket["first_round_numerator"] / reviewed if reviewed else None,
                         "owner_interventions": None})
            current += dt.timedelta(days=7)
    return {"rows": rows, "coverage": {"submission_dates": {"covered": submitted_coverage, "total": len(prs)},
                                          "merge_dates": {"covered": merged_coverage, "total": sum(r["outcome"] == "merged" for r in prs)}}}


def scan_output(text):
    if privacy.findings("metrics output", text, prose=True) or leaks.scan(text=text, label="metrics output"):
        raise CommandError("metrics: generated output did not pass the privacy and secret scans; no output written")


def public_files(root, *, config=None, records_root=None, suite_report=None):
    """Project saved records into the same scanned static assets used by the CLI."""
    from factory.core.metrics_site import render

    records = pathlib.Path(records_root or root).resolve()
    snapshot = read_snapshot(records, load_config(records), suite_report)
    result = compute(snapshot, CONFIG if config is None else config)
    files = {"site/metrics.json": json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
             "site/index.html": render(result)}
    for text in files.values():
        scan_output(text)
    return files


def command(args):
    from factory.core.metrics_site import render

    root = pathlib.Path(args.records_root).resolve() if args.records_root else ROOT
    try:
        config = load_config(root)
        snapshot = read_snapshot(root, config, args.suite_report, args.history_cache)
        # Public surface configuration belongs to the running factory, not the historical records being read.
        result = compute(snapshot, CONFIG)
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        page = render(result) if args.site else None
        scan_output(text)
        if page:
            scan_output(page)
        output = pathlib.Path(args.output)
        site = pathlib.Path(args.site) if args.site else None
        if site and output.resolve() == (site / "index.html").resolve():
            raise CommandError("metrics: the JSON output and site index must be different files")
        output.parent.mkdir(parents=True, exist_ok=True)
        write_json(output, result)
        if site:
            site.mkdir(parents=True, exist_ok=True)
            (site / "index.html").write_text(page, encoding="utf-8")
    except (OSError, ValueError, TypeError):
        raise CommandError("metrics: unreadable configuration, records or output destination") from None
    print(f"{'Metric':<40}{'Value':<22}Coverage")
    for key in ("first_round_approval_share", "prs_opened", "prs_merged", "prs_open", "merge_rate", "median_lead_hours", "shift_completion_rate"):
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
    parser = sub.add_parser("metrics", help="read saved evidence and export privacy-scanned portfolio metrics (no network)")
    parser.add_argument("--output", "-o", default="metrics/metrics.json", help="JSON output path (default: metrics/metrics.json)")
    parser.add_argument("--records-root", help="read another factory checkout and its configured records without updating them")
    parser.add_argument("--history-cache", help="read a separate PR history cache offline (default: beside the PR ledger)")
    parser.add_argument("--site", help="also write a self-contained static index.html to this directory")
    parser.add_argument("--suite-report", help="full-suite JSON produced by tests/run_suite.py --json; no tests are run by metrics")
    parser.set_defaults(fn=command)
    from factory.core.metrics_history import register as register_history
    register_history(parser)
