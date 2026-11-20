"""Claude Code Stop hook: an agent may not end its turn while it claims work is ready that the
gate rejects, or while a closed PR is waiting for its lesson.

Ready items are re-gated without network-heavy checks (hygiene, review, red→green bound to the
current head). After three consecutive gate failures the stop is allowed and the reason is
recorded. Hook crashes and timeouts always block.
"""
import json
import signal
import sys
from contextlib import contextmanager

from factory.core import clones, ledger
from factory.util import Item, ROOT, iso, path_from_config, read_json, write_json


@contextmanager
def deadline(seconds):
    def interrupt(signum, frame):
        raise KeyboardInterrupt(f"hook interrupted by signal {signum}")

    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGTERM, signal.SIGALRM)}
    timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *timer)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def ready_items():
    for meta_path in path_from_config("runs_dir").glob("*/*/item.json"):
        meta = read_json(meta_path)
        if meta.get("status") == "ready":
            yield Item(meta_path.parent)


def problems():
    from factory.core import gate

    out = []
    for item in ready_items():
        if clones.released(item) is not None:
            out.append(f"{item.dir.name}: clone released; run bin/factory clone ensure --item {item.dir} before finishing it")
            continue
        rep = gate.Report()
        gate.core_checks(item, rep, item.head_sha())
        for r in rep.rows:
            if not r["ok"]:
                out.append(f"{item.dir.name}: {r['check']} — {r['detail']} (fix: {r['fix']})")
    for r in ledger.missing_lessons():
        out.append(f"{r['url']} was closed or had no maintainer response without a lesson; run bin/factory lesson add --pr {r['url']}")
    return out


def check(payload):
    if not isinstance(payload, dict) or type(payload.get("stop_hook_active", False)) is not bool:
        raise ValueError("invalid Stop hook payload")
    state_path = ROOT / ".stop-state.json"
    state = read_json(state_path, {"bounces": 0})
    if not isinstance(state, dict) or type(state.get("bounces", 0)) is not int or state.get("bounces", 0) < 0:
        raise ValueError("invalid Stop retry state")
    found = problems()
    if not found:
        write_json(state_path, {"bounces": 0, "at": iso()})
        return 0
    state["bounces"] = state.get("bounces", 0) + 1 if payload.get("stop_hook_active") else 1
    state["at"] = iso()
    state["last"] = found
    write_json(state_path, state)
    if state["bounces"] > 3:
        print(json.dumps({"systemMessage": "factory stop-check gave up after 3 bounces; unresolved: " + " | ".join(found[:5])}))
        return 0
    reason = "Not done yet. Items marked ready fail the gate, or lessons are missing:\n- " + "\n- ".join(found[:12])
    reason += "\nFix them, or set the item status back to 'preparing' with a note on why it is blocked."
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def main():
    try:
        with deadline(270):
            return check(json.load(sys.stdin))
    except (Exception, KeyboardInterrupt, SystemExit) as error:
        print(json.dumps({"decision": "block", "reason": f"Cannot validate Stop hook: {error}"}))
        return 0
