"""When each role started and finished on an item, as the CLI saw it."""
import fcntl

from factory.util import now, parse_iso, read_json, write_json

FILE_NAME = "role-timeline.json"
LOCK_NAME = ".role-timeline.lock"


def events(item):
    data = read_json(item.file(FILE_NAME), [])
    return data if isinstance(data, list) else []


def mark(item, event, **data):
    with item.file(LOCK_NAME).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            rows = events(item)
            rows.append({"at": now().isoformat(), "event": event, **data})
            write_json(item.file(FILE_NAME), rows)
            return rows[-1]
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def elapsed(item, start_event):
    """Seconds since the latest start_event, or None when it never happened."""
    for row in reversed(events(item)):
        if row.get("event") == start_event:
            return (now() - parse_iso(row["at"])).total_seconds()
    return None
