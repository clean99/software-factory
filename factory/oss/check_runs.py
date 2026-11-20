"""A rollup can hold several runs of one check: a duplicate cancelled because a newer run superseded it, or a re-run.
Only the run that finished last says whether the check fails."""
import re

from factory.util import parse_iso

FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}
STAMPS = ("completedAt", "completed_at", "startedAt", "started_at", "updated_at", "created_at")


def name(check):
    return check.get("name") or check.get("context") or ""


def outcome(check):
    return (check.get("conclusion") or check.get("state") or "").upper()


def finished_at(check):
    stamp = next((check[key] for key in STAMPS if check.get(key)), None)
    try:
        value = parse_iso(stamp)
    except (AttributeError, TypeError, ValueError):
        return None
    return value if value.tzinfo else None


def last_runs(runs):
    """Runs that finished last: by timestamp when every run has one, else by list order. A tie keeps them all."""
    stamps = [finished_at(run) for run in runs]
    if None in stamps:
        return runs[-1:]
    last = max(stamps)
    return [run for run, stamp in zip(runs, stamps) if stamp == last]


def latest(checks, by_workflow=True):
    groups = {}
    for check in checks:
        key = ((check.get("workflowName") or "") if by_workflow else "", name(check))
        groups.setdefault(key, []).append(check)
    return [run for runs in groups.values() for run in last_runs(runs)]


def failing_runs(checks, by_workflow=True):
    completed = [check for check in checks if outcome(check) in FAILED | PASSED]
    return [run for run in latest(completed, by_workflow) if outcome(run) in FAILED]


def failing(checks, by_workflow=True):
    """Names whose last completed run failed; pending reruns do not clear a failure."""
    return {name(run) for run in failing_runs(checks, by_workflow)}


def pending(check):
    return (check.get("status") or check.get("state") or "").upper() in {"IN_PROGRESS", "QUEUED", "PENDING", "EXPECTED", "WAITING", "REQUESTED"}


def _check_url(check):
    return check.get("detailsUrl") or check.get("details_url") or check.get("targetUrl") or check.get("target_url") or ""


def source_id(check):
    if check.get("databaseId") or check.get("id"):
        return check.get("databaseId") or check["id"]
    url = _check_url(check)
    match = re.search(r"/(?:job|check-runs)/(\d+)(?:/|$)", url) or re.search(r"/runs/(\d+)(?:/|$)", url)
    return match[1] if match else None


def job_detail(check, head, on_base):
    url = _check_url(check)
    match = re.search(r"/actions/runs/(\d+)", url)
    return {"name": name(check), "workflow": check.get("workflowName") or "", "check_id": source_id(check),
            "run_id": check.get("run_id") or (match[1] if match else None), "head_sha": head,
            "failing_on_base": on_base, "url": url, "outcome": outcome(check)}
