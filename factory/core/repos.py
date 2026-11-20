"""Current names of repos GitHub renamed or transferred (facebook/lexical is meta/lexical now).

GitHub redirects the old URLs, so the ledger holds rows under both names. Anything that groups or
compares repos goes through canonical() so one repo is never counted on two keys; the ledger rows
keep the name they were recorded under. `check` makes read-only GitHub calls and never edits
factory.config.json: a rename it finds is added to repo_renames by hand.
"""
import re

from factory.core import ledger
from factory.util import CONFIG, GH_READ_TIMEOUT, CommandError, gh_json, path_from_config

NAME = re.compile(r"[\w.-]+/[\w.-]+")
WHERE = "repo_renames in factory.config.json"


def renames():
    """The old -> current map keyed by the lowercased old name; refuses a map it cannot apply unambiguously."""
    table = CONFIG.get("repo_renames", {})
    if not isinstance(table, dict):
        raise SystemExit(f"{WHERE} must be an object mapping old owner/name to current owner/name")
    lowered = {}
    for old, new in table.items():
        if not (isinstance(new, str) and NAME.fullmatch(old) and NAME.fullmatch(new)):
            raise SystemExit(f"{WHERE} maps {old!r} to {new!r}; both must be owner/name")
        if lowered.get(old.lower(), new).lower() != new.lower():
            raise SystemExit(f"{WHERE} maps {old} twice, to {lowered[old.lower()]} and {new}; keep one")
        lowered[old.lower()] = new
    for old, new in lowered.items():
        if new.lower() in lowered:
            raise SystemExit(f"{WHERE} maps {old} to {new}, which it maps again to {lowered[new.lower()]}; "
                             f"map {old} straight to the current name")
    return lowered


def canonical(name):
    """The current owner/name of a renamed repo, matched case-insensitively; any other value unchanged."""
    return renames().get(name.lower(), name) if isinstance(name, str) else name


def ledger_names():
    path = path_from_config("ledger_file")
    if not path.exists():
        raise SystemExit(f"ledger {path} not found; there are no repo names to check")
    try:
        rows = ledger.load()
    except (OSError, ValueError) as error:
        raise SystemExit(f"ledger {path} is unreadable ({error}); repair it before checking renames") from None
    if not isinstance(rows, list) or not all(isinstance(r, dict) and isinstance(r.get("repo"), str) for r in rows):
        raise SystemExit(f"ledger {path} has rows without a repo name; repair it before checking renames")
    names = {}
    for r in rows:
        names.setdefault(r["repo"].lower(), r["repo"])
    return sorted(names.values(), key=str.lower)


def current_name(name):
    data = gh_json("api", f"repos/{name}", timeout=GH_READ_TIMEOUT)
    full = data.get("full_name") if isinstance(data, dict) else None
    if not isinstance(full, str) or not NAME.fullmatch(full):
        raise ValueError(f"repos/{name} came back without a full_name")
    return full


def check():
    """Ledger repos whose current GitHub name differs from what canonical() gives them."""
    names = ledger_names()
    renames()
    unmapped, errors = [], []
    for name in names:
        try:
            current = current_name(name)
        except (CommandError, ValueError) as error:
            errors.append({"repo": name, "error": str(error)[:300]})
            continue
        if canonical(name).lower() != current.lower():
            unmapped.append({"repo": name, "maps_to": canonical(name), "current": current})
    return {"ok": not unmapped and not errors, "checked": len(names), "unmapped": unmapped, "errors": errors}
