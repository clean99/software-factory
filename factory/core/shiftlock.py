"""One shift at a time: a lease in ROOT/.shift-lock.json that concurrent sessions take before running a shift."""
import contextlib
import datetime as dt
import fcntl
import os
import secrets

from factory.util import ROOT, iso, now, parse_iso, read_json, write_json

STATE = ".shift-lock.json"
SIDECAR = ".shift-lock.lock"
# Set by schedule/agent-shift.py for a contribution session: when its run began, so resumes keep one shift count.
RUN_STARTED = "FACTORY_RUN_STARTED_AT"


@contextlib.contextmanager
def locked():
    with open(ROOT / SIDECAR, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield ROOT / STATE
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load(path):
    try:
        state = read_json(path)
        if state is None or (isinstance(state["owner"], str) and state["owner"] and parse_iso(state["expires_at"]).tzinfo):
            return state
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    raise ValueError(f"{path.name} cannot be read; inspect it, then clear it with `bin/factory lock release --force`")


def describe(state, t):
    remaining = (parse_iso(state["expires_at"]) - t).total_seconds() / 60
    return {"owner": state["owner"], "acquired_at": state.get("acquired_at"), "expires_at": state["expires_at"],
            "remaining_minutes": max(0, round(remaining)), "generation": state.get("generation")}


def live(state, t):
    return state if state and parse_iso(state["expires_at"]) > t else None


def window_start(t, path=None):
    """When the current contribution shift began, for counting its new PRs.

    The scheduler's run start comes first: it holds across session resumes and a re-taken lease. Without it, the
    live lease's acquisition time; outside any shift, the Asia/Singapore midnight before t, so ad hoc submits share
    one count per day. A malformed or empty run start raises rather than widening the window."""
    text = os.environ.get(RUN_STARTED)
    if text is not None:
        try:
            started = parse_iso(text)
        except ValueError:
            started = None
        if started is None or started.tzinfo is None:
            raise ValueError(f"{RUN_STARTED} must be an ISO time with a zone offset, not {text!r}")
        return started
    held = live(load(path or ROOT / STATE), t)
    if held and held.get("acquired_at"):
        return parse_iso(held["acquired_at"])
    return day_start(t)


def day_start(t):
    """The Asia/Singapore midnight that begins t's day, whatever the machine zone."""
    from zoneinfo import ZoneInfo

    from factory.core.metrics import ACTIVITY_ZONE

    zone = ZoneInfo(ACTIVITY_ZONE)
    return dt.datetime.combine(t.astimezone(zone).date(), dt.time(), zone)


def acquire(owner, ttl_hours, commit_baseline=None):
    t = now()
    with locked() as path:
        state = load(path)
        held = live(state, t)
        if held and held["owner"] != owner:
            return {"ok": False, "reason": f"held by {held['owner']}", "holder": describe(held, t)}
        record = {"owner": owner, "acquired_at": held.get("acquired_at", iso(t)) if held else iso(t),
                  "expires_at": iso(t + dt.timedelta(hours=ttl_hours)), "generation": secrets.token_hex(16)}
        baseline = held.get("commit_baseline") if held else commit_baseline
        if baseline is not None:
            record["commit_baseline"] = baseline
        write_json(path, record)
    result = {"ok": True, "refreshed": bool(held), "holder": describe(record, t)}
    if state and not held and state["owner"] != owner:
        result["expired_holder"] = state["owner"]
    return result


def renew(owner, generation, ttl_hours):
    t = now()
    with locked() as path:
        state = load(path)
        if not state or not generation or state.get("generation") != generation:
            return {"ok": False, "reason": "shift was superseded; acquire a new shift before continuing"}
        if state["owner"] != owner:
            return {"ok": False, "reason": f"held by {state['owner']}", "holder": describe(state, t)}
        if not live(state, t):
            return {"ok": False, "reason": "shift lease expired; acquire a new shift before continuing"}
        state["expires_at"] = iso(t + dt.timedelta(hours=ttl_hours))
        write_json(path, state)
    return {"ok": True, "refreshed": True, "holder": describe(state, t)}


def verify(generation):
    if not generation:
        return False
    try:
        with locked() as path:
            state = live(load(path), now())
            return bool(state and state.get("generation") == generation)
    except (OSError, ValueError, OverflowError):
        return False


def release(owner, force, generation=None):
    with locked() as path:
        if not path.exists():
            return {"ok": True, "released": None}
        try:
            state = load(path)
        except ValueError:
            if not force:
                raise
            state = {"owner": None}
        if generation is not None and state.get("generation") != generation:
            return {"ok": False, "reason": "shift was superseded; do not release the current holder's lease"}
        if state["owner"] != owner and not force:
            return {"ok": False, "reason": f"held by {state['owner']}; only the holder releases it without --force",
                    "holder": describe(state, now())}
        path.unlink()
    return {"ok": True, "released": state["owner"]}


def status():
    t = now()
    with locked() as path:
        state = load(path)
    held = live(state, t)
    if held:
        return {"ok": True, "free": False, "holder": describe(held, t)}
    return {"ok": True, "free": True, **({"expired_holder": state["owner"]} if state else {})}


def run(action, owner=None, ttl_hours=4, force=False, generation=None, commit_baseline=None):
    owner = (owner or "").strip()
    if action != "status" and not owner:
        return {"ok": False, "reason": f"lock {action} needs --owner"}
    if action in {"acquire", "renew"} and not ttl_hours > 0:
        return {"ok": False, "reason": "--ttl-hours must be positive"}
    try:
        if action == "acquire":
            return acquire(owner, ttl_hours, commit_baseline=commit_baseline)
        if action == "renew":
            return renew(owner, generation, ttl_hours)
        if action == "release":
            return release(owner, force, generation)
        return status()
    except (OSError, ValueError, OverflowError) as error:
        return {"ok": False, "reason": str(error)}
