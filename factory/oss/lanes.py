"""Read lane membership, states and tie-breaking order for scouting."""
import datetime as dt

from factory.core.repos import NAME, canonical
from factory.util import ROOT, read_json


STATES = {"active", "paused", "stopped"}


def load():
    path = ROOT / "lines" / "oss" / "lanes.json"
    data = read_json(path)
    if not isinstance(data, dict) or set(data) != {"depth", "explore"}:
        raise ValueError("lines/oss/lanes.json needs depth and explore arrays; restore the lane data")
    seen = set()
    for lane in ("depth", "explore"):
        if not isinstance(data[lane], list):
            raise ValueError(f"lines/oss/lanes.json {lane} must be an array; fix the lane data")
        for entry in data[lane]:
            if (not isinstance(entry, dict) or not isinstance(entry.get("repo"), str)
                    or not NAME.fullmatch(entry["repo"]) or not isinstance(entry.get("state"), str) or entry["state"] not in STATES
                    or not isinstance(entry.get("reason"), str) or not entry["reason"].strip()):
                raise ValueError(f"lines/oss/lanes.json {lane} needs repo, state, reason and since for each entry")
            try:
                dt.date.fromisoformat(entry["since"])
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"lines/oss/lanes.json {entry['repo']} needs since in YYYY-MM-DD format") from None
            name = canonical(entry["repo"])
            if name.lower() in seen:
                raise ValueError(f"lines/oss/lanes.json lists {name} twice; keep one canonical entry")
            seen.add(name.lower())
            entry["repo"] = name
    return data


def order(lane=None):
    data = load()
    selected = (lane,) if lane is not None else ("depth", "explore")
    return [entry["repo"] for name in selected for entry in data[name] if entry["state"] == "active"]


def state(repo):
    name = canonical(repo).lower()
    for entries in load().values():
        for entry in entries:
            if entry["repo"].lower() == name:
                return entry["state"]
    return None


def cmd_lanes(args):
    rows = [("Lane", "Repo", "State", "Since", "Reason")]
    for lane, entries in load().items():
        rows.extend((lane, entry["repo"], entry["state"], entry["since"], entry["reason"]) for entry in entries)
    widths = [max(len(row[index]) for row in rows) for index in range(4)]
    for row in rows:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)) + "  " + row[-1])
    return 0


def register(sub):
    parser = sub.add_parser("lanes", help="show depth order and active, paused or stopped repo states")
    parser.set_defaults(fn=cmd_lanes)
