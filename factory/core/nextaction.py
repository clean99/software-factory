"""Project the work still owed from item evidence, observations and destination receipts."""
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import sys

from factory.core import clean, clones, ledger, lessons, metrics, metrics_history, review, shiftlock, verdicts
from factory.core.commit import LIMIT, LOG_POLICY
from factory.core.repos import canonical
from factory.oss import followup, lanes, plan, profile_readme, sync
from factory.util import CONFIG, ROOT, CommandError, Item, iso, now, parse_iso, path_from_config, read_json, run, write_json

ZONE = dt.timezone(dt.timedelta(hours=8))
ACKS = ".shift-acks.json"
GUARD = ".shift-next.lock"
ACKABLE = {"disk", "ci-triage", "stale-head", "issue-taken", "issue-confirmed", "report"}
DISK_FLOOR_GIB = 6
FEEDBACK = {"changes-requested", "maintainer-review", "maintainer-comment", "bot-finding", "conflict"}
ORDER = {kind: index for index, kind in enumerate(("push", "disk", "repair", "owner-action", "ci-triage", "stale-head", "issue-taken",
          "issue-confirmed", "lesson", "coordinator-review", "ship", "propose", "stalled-item", "takeover", "held", "records", "wave", "route-lessons", "release", "commit", "measure", "report"))}
ITEM_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError)
SCOUT_COOLDOWN = dt.timedelta(days=3)
ITEM_STALL = dt.timedelta(days=5)
HISTORY_REFRESH = dt.timedelta(hours=12)
# Printing a brief records a worker start, not completed work; exclude role-timeline.json.
PROGRESS_FILES = ("item.json", "demand.json", "value-review.json", "proposal.md", "verify.md", "validation.md",
                  "redgreen.json", "pr.md", "review.md", "review.json", "coordinator-review.md",
                  "coordinator-review.json", "takeover.json", "receipts")


def identifier(kind, target, inputs):
    encoded = json.dumps([kind, target, inputs], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return kind + "-" + hashlib.sha256(encoded.encode()).hexdigest()[:10]


def action(kind, target, inputs, title, why, commands, done_when, role="coordinator", brief=None):
    return {"id": identifier(kind, target, inputs), "kind": kind, "role": role, "title": title,
            "why": why, "commands": commands, "brief": brief, "done_when": done_when, "target": target}


def command(*args):
    return shlex.join(["bin/factory", *map(str, args)])


def local_path(value):
    path = pathlib.Path(value)
    path = path if path.is_absolute() else ROOT / path
    runs = path_from_config("runs_dir").resolve()
    resolved = path.resolve()
    if resolved.is_relative_to(runs):
        if resolved.is_dir():
            return resolved
        raise ValueError(f"{path} is missing; restore the item directory or correct the ledger row")
    for index in range(1, len(path.parts) - 1):
        candidate = runs.joinpath(*path.parts[index:]).resolve()
        if candidate.is_relative_to(runs) and candidate.is_dir():
            return candidate
    raise ValueError(f"{path} has no matching day/item tail under {runs}; restore the local item or correct the ledger row")


@contextlib.contextmanager
def evidence_file(path):
    try:
        yield
    except ITEM_ERRORS as error:
        raise ValueError(f"{path}: {error}; repair this evidence file before continuing the item") from None


def object_data(path):
    with evidence_file(path):
        data = read_json(path, {})
        if not isinstance(data, dict):
            raise ValueError("expected one JSON object")
        return data


def local_item(value):
    path = local_path(value)
    with evidence_file(path / "item.json"):
        try:
            item = Item(path)
        except SystemExit as error:
            raise ValueError(str(error)) from None
        if not isinstance(item.meta, dict):
            raise ValueError("expected one JSON object")
        if not isinstance(item.meta.get("repo"), str) or not item.meta["repo"]:
            raise ValueError("set repo to owner/name")
        for key in ("status", "pr_url"):
            if item.meta.get(key) is not None and not isinstance(item.meta[key], str):
                raise ValueError(f"set {key} to a string")
        int(item.meta.get("schema", 1))
        return item


def repair_action(target, error):
    return action("repair", str(target), str(error), f"Repair {target}", str(error),
                  ["Read docs/repairs.md#clone-recovery and restore the named file or correct its item/ledger reference without discarding other work."],
                  "The named file is readable and the item can be projected again.")


def item_target(item):
    try:
        return str(item.dir.relative_to(ROOT))
    except ValueError:
        return str(item.dir)


def fingerprint(item):
    try:
        head, error = item.head_sha(), None
    except (CommandError, OSError, KeyError) as exc:
        head, error = None, str(exc)
    data = {"head": head, "head_error": error}
    suffix = "json" if item.schema >= 2 else "md"
    for key, filename, reader in (("pr_sha256", "pr.md", verdicts.pr_sha256),
                                  ("review", f"review.{suffix}", verdicts.read_review),
                                  ("coordinator", f"coordinator-review.{suffix}", verdicts.read_coordinator)):
        with evidence_file(item.file(filename)):
            data[key] = reader(item)
    return data


def inactive(item, rows):
    row = rows.get(item.meta.get("pr_url"))
    return item.meta.get("status") in plan.INACTIVE or bool(row and row.get("status") in ledger.DONE)


def read_acks():
    data = read_json(ROOT / ACKS, {})
    if not isinstance(data, dict):
        raise ValueError(f"{ACKS} must contain an object; repair it before acknowledging work")
    return data


def observations(observe):
    path = path_from_config("ledger_file").with_name("followup-events.jsonl")
    saved = []
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                entry = json.loads(line)
                if not isinstance(entry, dict) or not all(k in entry for k in ("key", "kind", "url", "detail", "at")):
                    raise ValueError("followup-events.jsonl has an invalid event; repair it before refreshing")
                saved.append(entry)
    live = followup.events() if observe and path.exists() else []
    by_key = {}
    for entry in saved + live:
        if entry["kind"] in {"maintainer-comment", "maintainer-review", "community-comment", "community-review"}:
            by_key.setdefault(entry["key"], entry)
        else:
            by_key[entry["key"]] = entry
    source = ("Using saved followup observations; run bin/factory next --refresh to observe GitHub."
              if not observe or not path.exists() or not live else "Using persisted observations from the last followup sweep.")
    ordered = sorted(by_key.values(), key=lambda e: (parse_iso(e["at"]), e["key"]))
    return recover_merger_feedback(ordered), source


def merger_error(event):
    data = detail(event)
    return event["kind"] == "followup-error" and (
        data.get("operation") == "repo-mergers" or
        (not data.get("operation") and data.get("text", "").startswith("recent mergers could not be read:")))


def recover_merger_feedback(ordered):
    """Recover failed classifications in memory, retaining source identity and first observation time."""
    reads, community, completed = {}, {}, {}
    projected = {e["key"]: e for e in ordered}
    for entry in ordered:
        if entry["kind"] == "repo-mergers-read" and detail(entry).get("operation") in {None, "repo-mergers"}:
            reads.setdefault(entry["url"], []).append(entry)
        elif entry["kind"] == "feedback-read":
            completed.setdefault(entry["url"], []).append(entry)
        elif entry["kind"] in {"community-comment", "community-review"}:
            match = re.fullmatch(r"(https://github.com/[^/]+/[^/]+)/pull/\d+", entry["url"])
            if match:
                community.setdefault(match[1], []).append(entry)
    for error in ordered:
        if not merger_error(error):
            continue
        failed_at = parse_iso(error["at"])
        later = [e for e in reads.get(error["url"], []) if parse_iso(e["at"]) > failed_at]
        if not later:
            continue
        recovery = next((e for e in later if isinstance(detail(e).get("logins"), list)
                         and all(isinstance(login, str) for login in detail(e)["logins"])), None)
        mergers = {login.lower() for login in detail(recovery)["logins"]} if recovery else None
        consumer = detail(error).get("consumer")
        # Another reader's success cannot finish a producer that has not emitted its feedback.
        complete = not consumer or any(
            detail(e).get("merger_error") == error["key"] or
            (parse_iso(e["at"]) > failed_at and parse_iso(detail(e)["since"]) <= failed_at)
            for e in completed.get(consumer, []))
        affected = [e for e in community.get(error["url"], [])
                    if detail(e).get("merger_error") == error["key"] or
                    ("merger_error" not in detail(e) and parse_iso(e["at"]) >= failed_at)]
        for entry in affected:
            prefix = entry["url"] + "#" + entry["kind"] + "#"
            if not entry["key"].startswith(prefix):
                complete = False
                continue
            kind = entry["kind"].replace("community-", "maintainer-", 1)
            key = entry["url"] + "#" + kind + "#" + entry["key"][len(prefix):]
            existing = projected.get(key)
            response = detail(entry)
            login = community_author(entry)
            if not existing and (mergers is None or not login):
                complete = False
                continue
            if existing or followup.response_kind({"author": {"login": login}, "body": response.get("body")}, mergers) == "maintainer":
                promoted = {**entry, "key": key, "kind": kind,
                            "detail": {**response, "text": response.get("text", "").replace("COMMUNITY ", "MAINTAINER ", 1)}}
                projected.pop(entry["key"], None)
                # A reread must not move a handled source past its reply receipt.
                projected[key] = min((existing, promoted), key=lambda e: parse_iso(e["at"])) if existing else promoted
        if complete:
            projected.pop(error["key"], None)
    return sorted(projected.values(), key=lambda e: (parse_iso(e["at"]), e["key"]))


def community_author(entry):
    data = detail(entry)
    if data.get("author_login"):
        return data["author_login"]
    label = "COMMUNITY " + entry["kind"].split("-", 1)[1].upper() + " " + entry["url"]
    match = re.match(re.escape(label) + r"(?: [A-Z_]+)? by ([^\s:]+):", data.get("text", ""))
    return match[1] if match else None


def detail(event):
    value = event.get("detail")
    return value if isinstance(value, dict) else {"text": str(value or "")}


def event_head(event):
    data = detail(event)
    return data.get("head_sha") or data.get("pr_head") or next(
        (j.get("head_sha") for j in data.get("jobs", []) if j.get("head_sha")), None)


def current_event(event, rows):
    row = rows.get(event["url"])
    if row and row.get("status") in ledger.DONE:
        return False
    if row and event_head(event) and row.get("head_sha") and event_head(event) != row["head_sha"]:
        return False
    kind = event["kind"]
    if row and kind in {"cla", "awaiting-cla"} and row.get("cla_state") == "passed":
        return False
    newer_observation = bool(row and row.get("last_check") and parse_iso(row["last_check"]) >= parse_iso(event["at"]))
    if kind == "followup-error" and newer_observation and parse_iso(row["last_check"]) > parse_iso(event["at"]):
        return False
    if row and kind in {"ci-failing", "pre-existing-failures"} and newer_observation:
        jobs = detail(event).get("jobs", [])
        names = {j["name"] for j in jobs if not j.get("failing_on_base") and not j.get("failing_on_other_prs")}
        if kind == "ci-failing" and names and not names.intersection((row.get("checks") or {}).get("failing", [])):
            return False
    return True


def owner_count():
    path = ROOT / "docs/owner-actions.md"
    if not path.exists():
        return 0
    count, opened = 0, False
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            opened = line[3:].strip().lower() in {"open", "待处理", "未完成"}
        if not opened:
            continue
        if re.match(r"\s*[-*] \[ \] ", line):
            count += 1
        elif line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]) and len(cells) > 1 and not cells[1].startswith("~~"):
                count += 1
    return count


def report_line(rows=None, at=None):
    rows = ledger.load() if rows is None else rows
    today = (at or now()).astimezone(ZONE).date()

    def today_at(value):
        return bool(value) and parse_iso(value).astimezone(ZONE).date() == today

    merged = sum(r.get("status") == "merged" and today_at(r.get("merged_at")) for r in rows)
    submitted = sum(today_at(r.get("submitted_at")) for r in rows)
    opened = sum(r.get("status") not in ledger.DONE for r in rows)
    return f"今日合入 {merged} 个、今日提交 {submitted} 个、当前开着 {opened} 个、待本人处理 {owner_count()} 项"


def follow_blocker(item, events):
    """A saved failed attempt holds the same feedback/head, not unrelated new work."""
    if not events:
        return None
    data = object_data(item.file("follow.json"))
    blockers = data.get("blockers")
    if not blockers:
        return None
    if not isinstance(blockers, list) or not all(isinstance(text, str) and text.strip() for text in blockers):
        raise ValueError(f"{item.file('follow.json')}: blockers must be a list of non-empty strings")
    if not {event["key"] for event in events}.issubset(set(data.get("event_keys", []))):
        return None
    if data.get("head"):
        try:
            head = item.head_sha()
        except (CommandError, OSError, KeyError) as error:
            raise ValueError(f"could not check the blocked head: {error}") from None
        if data["head"] != head:
            return None
    return {"item": item_target(item), "blockers": blockers,
            "reopen_when": data.get("reopen_when") or "Record evidence resolving the blockers in follow.json and clear its blockers list."}


def follow_prepared(item, events):
    data = object_data(item.file("follow.json"))
    keys = set(data.get("event_keys", []))
    requests = data.get("requests") or []
    if not {e["key"] for e in events}.issubset(keys) or not requests:
        return False
    if not all(r.get("request") and r.get("disposition") and r.get("evidence") for r in requests):
        return False
    reply = item.dir / (data.get("reply_file") or "")
    if not reply.resolve().is_relative_to(item.dir) or not reply.is_file():
        return False
    result = verdicts.read_review(item)
    inputs_at = max(path.stat().st_mtime_ns for path in (item.file("follow.json"), reply, item.file("pr.md")) if path.is_file())
    return bool(result and result["verdict"] in verdicts.VERDICTS
                and verdicts.bound(item, item.head_sha(), result["head"])
                and (item.schema < 2 or result.get("pr_sha256") == verdicts.pr_text_sha256(item))
                and item.file(result["source"]).stat().st_mtime_ns >= inputs_at)


def follow_replied(item, events):
    data = object_data(item.file("follow.json"))
    if not {e["key"] for e in events}.issubset(set(data.get("event_keys", []))):
        return False
    replies = item.meta.get("replies", []) + item.meta.get("thread_replies", [])
    return any(r.get("file") == data.get("reply_file") and r.get("url") and r.get("at")
               and all(parse_iso(r["at"]) >= parse_iso(e["at"]) for e in events) for r in replies)


def parking(item):
    return (f"Or park it: bin/factory park {item_target(item)} --reason '<evidence-backed reason>' "
            "(add --blocked when it waits on something outside the factory).")


def review_done(item, text, part="review"):
    if item.schema >= 2:
        text += "\nValidate the completed evidence with\n" + command("review", "check", "--item", item_target(item), part)
    return text


def review_action(item, why):
    target = item_target(item)
    brief = command("brief", "coordinator-review", "--item", target)
    return action("coordinator-review", target, fingerprint(item), f"Review {item.dir.name}", why,
                  [brief], review_done(item, "The current head is ship_ready, or its coordinator verdict is fix-needed/drop.", "coordinator"), brief=brief)


def follow_ship(item):
    target = item_target(item)
    return action("ship", target, fingerprint(item), f"Publish the reviewed update for {item.dir.name}",
                  "Feedback is prepared and reviewed; an existing PR needs submit and a reply, not a new PR.",
                  [command("approve", target, "--note", CONFIG["standing_approval_note"]),
                   command("submit", target, "--dry-run"), command("submit", target),
                   f"Follow docs/repairs.md#open-pr-updates for body edits, approved replies and live head/body read-back."],
                  "The matching reply receipt is saved, or the PR has merged/closed.")


def changed_paths(pathspecs=()):
    argv = ["git", "--no-optional-locks", "-C", str(ROOT), "status", "--porcelain", "-z", "--untracked-files=all"]
    output = run(argv + (["--", *pathspecs] if pathspecs else [])).stdout
    fields, paths, index = output.split("\0"), [], 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        untracked = field[:2] == "??"
        paths.append((field[3:], untracked))
        if "R" in field[:2] or "C" in field[:2]:
            if index < len(fields) and fields[index]:
                paths.append((fields[index], untracked))
            index += 1
    return sorted(set(paths))


def commit_changes(include_runs=False):
    """Fingerprint factory records without opening unrelated root-level untracked files."""
    records, left_alone, item_folders = {}, [], {}
    root, runs = ROOT.resolve(), path_from_config("runs_dir").resolve()
    changed = changed_paths()
    ignored = set()
    if changed:
        checked = run(["git", "-C", str(ROOT), "check-ignore", "--no-index", "-z", "--stdin"],
                      input="\0".join(name for name, _ in changed) + "\0", check=False)
        if checked.returncode not in (0, 1):
            raise ValueError("could not check ignored record paths")
        ignored = set(checked.stdout.split("\0"))
    for name, untracked in changed:
        if name in ignored:
            left_alone.append(name)
            continue
        if untracked and "/" not in name:
            left_alone.append(name)
            continue
        path = root / name
        folder = None
        if path.is_relative_to(runs):
            parts = path.relative_to(runs).parts
            folder = runs / parts[0] / parts[1] if len(parts) >= 3 else None
            if folder and folder not in item_folders:
                item_folders[folder] = (folder / "item.json").exists()
            if not include_runs and not (folder and (item_folders[folder] or path.name == "item.json")):
                folder = None
                if untracked:
                    left_alone.append(name)
                    continue
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"changed path {name} leaves the checkout; inspect it before committing")
        if path.is_file() and path.stat().st_size > LIMIT:
            left_alone.append(name)
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        records[name] = [untracked, digest]
    return records, left_alone


def push_action():
    """Retry saved main commits before starting new work; do not fetch in an ordinary next."""
    def git(*args):
        return run(["git", "-C", str(ROOT), *args], check=False).stdout.strip()
    if git("symbolic-ref", "--short", "-q", "HEAD") != "main":
        return None
    if not git("rev-parse", "--verify", "refs/remotes/origin/main"):
        return None
    outgoing = git("rev-list", "origin/main..HEAD").splitlines()
    if not outgoing:
        return None
    return action("push", "factory-main", outgoing, "Push saved factory commits",
                  "main has commits absent from the saved origin/main ref; push if needed, then refresh that ref before new work.",
                  [command("push", "-C", ROOT, "--", "origin", "main"),
                   shlex.join(["git", "-C", str(ROOT), "fetch", "origin", "refs/heads/main:refs/remotes/origin/main"]),
                   "If access fails with -25293, follow docs/repairs.md#locked-keychain and close the shift without an owner action."],
                  "origin/main contains the saved commits.")


def commit_action():
    """Project changes since shift start; unchanged inherited records do not belong to this shift."""
    state = shiftlock.live(shiftlock.load(ROOT / shiftlock.STATE), now())
    baseline = state.get("commit_baseline") if state else None
    records, left_alone = commit_changes(include_runs=baseline is not None)
    inputs = [[name, value] for name, value in records.items() if baseline is None or baseline.get(name) != value]
    if not inputs:
        return None
    # Explicit files cannot pull inherited, ignored tracked or oversized siblings into git add.
    paths = sorted(name for name, _ in inputs)
    argv = ["commit", "-m", "<what changed>"]
    for path in paths:
        argv += ["--path", path]
    why = "Changed paths: " + ", ".join(paths)
    if left_alone:
        why += f"; left alone: {len(left_alone)} ignored, oversized or unrelated untracked paths; keep raw dumps local"
    return action("commit", "factory", inputs, "Commit the factory changes", why,
                  ["Factory records stay on main: do not create a shift branch; the printed bin/factory commit command also pushes.",
                   "Review git diff and git diff --cached for the selected paths; leave inherited records untouched. Unrelated staged changes are refused.",
                   LOG_POLICY, command(*argv)],
                  "This shift's selected records are committed; leave other workers' changes untouched.")


def retro_dates():
    dates = []
    for path in sorted((ROOT / "docs/retro").glob("*.md")):
        text = path.stem + "\n" + path.read_text()[:1000]
        for value in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text):
            try:
                dates.append(dt.date.fromisoformat(value))
                break
            except ValueError:
                continue
    return dates


def quarantine(at, name):
    return ROOT / "workspace/quarantine" / f"{name}-{at.astimezone(dt.timezone.utc):%Y%m%dT%H%M%SZ}"


def history_action(at):
    path = path_from_config("ledger_file").with_name("pr-history.json")
    if not path.resolve().is_relative_to((ROOT / "ledger").resolve()):
        return action("repair", "history-cache", ["location", str(path)], "Repair the saved history location",
                      f"The saved history beside the ledger ({path}) is outside this checkout's ledger directory, where bin/factory metrics history writes; "
                      "the public pages and the profile header would read a different file than the refresh writes.",
                      ["Move the ledger under ledger/ and point ledger_file there, keeping its contents; then run bin/factory next."],
                      "The ledger and its saved history are under this checkout's ledger directory.")
    try:
        cache = metrics_history.load_cache(path)
    except CommandError:
        relative = path.relative_to(ROOT)
        copy = quarantine(at, "pr-history").with_suffix(".json")
        return action("repair", "history-cache", ["unreadable", str(relative)], "Repair the saved pull request history",
                      f"{relative} cannot be read by bin/factory metrics history, which refuses to overwrite it; the refresh and the profile header wait for it.",
                      [shlex.join(["mkdir", "-p", str(copy.parent)]), shlex.join(["cp", str(relative), str(copy)]),
                       shlex.join(["git", "restore", "--source=HEAD", "--", str(relative)]),
                       "If the committed copy is unreadable too, remove the working file instead; bin/factory metrics history then starts a new cache. Run bin/factory next for the refresh."],
                      "bin/factory metrics history can read the saved history; the damaged copy stays under workspace/quarantine for inspection.")
    try:
        attempt = metrics.timestamp(cache.get("reconciliation", {}).get("checked_at"), exact=True)
    except (OverflowError, *ITEM_ERRORS):
        attempt = None
    if attempt is not None and at - attempt < HISTORY_REFRESH:
        return None
    stamp = attempt.isoformat() if attempt is not None else None
    relative = path.relative_to(ROOT)
    entry = action("records", "history", stamp, "Refresh saved pull request history",
                   f"Last attempt: {stamp or 'never'}; public counts, daily activity and the profile header come from this saved observation.",
                   [command("metrics", "history", "--cache", relative), command("commit", "-m", "Refresh saved pull request history", "--path", relative),
                    "Exit 3 from the refresh means GitHub could not be read completely: the cache keeps the last successful counts and records the attempt, so commit it either way."],
                   "The saved GitHub observation was attempted within the last twelve hours.")
    entry["observation"] = True
    return entry


def profile_header_action(profile):
    try:
        checkout = pathlib.Path(profile).parent
        if not checkout.exists():
            return action("records", "profile-header", "checkout", "Restore the profile checkout",
                          "The profile checkout is missing; restore it before comparing its published header with the retained snapshot.",
                          [command("sync", "profile", "--readme", profile)],
                          "The profile checkout exists with an origin/main README.")
        if not (checkout / ".git").exists():
            moved = quarantine(now(), checkout.name)
            return action("repair", "profile-checkout", str(checkout), "Move the unexpected profile directory aside",
                          f"{checkout} exists without Git metadata, so sync profile cannot restore the profile clone there.",
                          [shlex.join(["mkdir", "-p", str(moved.parent)]), shlex.join(["mv", str(checkout), str(moved)]),
                           command("sync", "profile", "--readme", profile)],
                          "The profile checkout is a Git clone with an origin/main README; the moved directory stays under workspace/quarantine for inspection.")
        text = run(["git", "-C", str(checkout), "show", "refs/remotes/origin/main:README.md"],
                   env={**os.environ, "GIT_NO_LAZY_FETCH": "1"}, timeout=5).stdout
        current = profile_readme.block(text, profile_readme.PORTFOLIO_START, profile_readme.PORTFOLIO_END)
        if current is None:
            return None
        user = CONFIG["github_user"]
        expected = "\n" + profile_readme.portfolio(profile_readme.ledger_rows(user), user)
    except (CommandError, *ITEM_ERRORS):
        return None
    if current == expected:
        return None
    return action("records", "profile-header", expected, "Refresh profile header",
                  "The published profile header differs from the current retained public snapshot; no merge receipt is owed for this refresh.",
                  [shlex.join(["git", "-C", str(checkout), "pull", "--ff-only"]),
                   command("sync", "profile", "--readme", profile, "--write"), command("profile-check", "--readme", profile),
                   'Scan, commit and push the profile repository as docs/repairs.md#profile-repository describes with commit message "Refresh profile header"; verify the remote commit.'],
                  "The profile checkout's origin/main README carries the generator's header for the current retained snapshot.", role="record")


def record_actions(rows, at, load_item):
    by_url = {r["url"]: r for r in rows}
    pending = sync.status()
    profile = path_from_config("repos_dir") / (CONFIG["github_user"] + "__" + CONFIG["github_user"]) / "README.md"
    result, audits = [], []
    for dest in sync.DESTINATIONS:
        urls, teaching = [], []
        for url in pending.get(dest, []):
            row = by_url.get(url, {})
            receipt = sync.records(row).get(dest) or {}
            if not isinstance(receipt, dict):
                receipt = {}
            if sync.unverified(receipt):
                audits.append([url, dest, receipt])
                continue
            if dest == "notebook" and receipt.get("deferred") and receipt.get("at"):
                age = (at - parse_iso(receipt["at"])).total_seconds()
                if 0 <= age < 12 * 3600:
                    continue
            if dest == "teach" and row.get("item"):
                item = load_item(row["item"], url)
                if item is None:
                    continue
                try:
                    if object_data(item.file("record.json")).get("insight"):
                        teaching.append(command("brief", "record", "--pr", url))
                except ITEM_ERRORS as error:
                    result.append(repair_action(f"ledger row {url} ({item_target(item)})", error))
                    continue
            urls.append(url)
        if not urls:
            continue
        urls = sorted(set(urls))
        if dest == "profile":
            commands = ["Edit the matching groups[].repos[] entries in lines/oss/profile-groups.json before generation: "
                        "update each repo's blurb to describe the newly accepted work, not just its count; add a repo entry if absent. "
                        "For join_previous, check the shared blurb; for outside_block, update the hand-maintained README entry instead.",
                        command("sync", "profile", "--readme", profile),
                        "Review the preview against the accepted PRs: every new contribution must be described accurately; preserve older contributions.",
                        command("sync", "profile", "--readme", profile, "--write"),
                        command("profile-check", "--readme", profile),
                        "Scan, commit and push the profile repository as docs/repairs.md#profile-repository describes; verify the remote commit."]
        elif dest == "teach":
            commands = [command("sync", "interview", "--write"), "Review the generated contribution tables; preserve existing teaching text.", *teaching]
        else:
            commands = [command("sync", "notebook", "--out", "workspace/record-outbox"),
                        "Read the small Private notebook status note and its sha; write through the connector, then read back. Preserve the historical career note."]
        if dest == "profile":
            source = profile
        elif dest == "teach":
            source = "docs/interview"
        else:
            source = "<read-back note file>"
        label = ["--ref", "<commit>"] if dest == "profile" else []
        commands.append(command("sync", "mark", dest, "--file", source, *label))
        if dest == "notebook":
            commands.append("If the connector is unreachable, record the actual reason instead of a completed receipt:")
            if len(urls) <= 5:
                commands.extend(command("sync", "mark", dest, url, "--deferred", "<connector error and retry reason>") for url in urls)
            else:
                commands.append(command("sync", "mark", dest, "<pr-url>", "--deferred", "<connector error and retry reason>")
                                + f"  (once for each of the {len(urls)} PRs listed above)")
        inputs = [[url, by_url.get(url, {}).get("merged_at"), by_url.get(url, {}).get("phrase"), sync.records(by_url.get(url, {})).get(dest)] for url in urls]
        result.append(action("records", dest, inputs, f"Update {dest} records", "Missing destination receipts: " + ", ".join(urls),
                             commands, "Each destination has a verified receipt; Private notebook deferrals return after 12 hours.", role="record"))
    if audits:
        result.append(action("records", "audit", sorted(audits, key=lambda x: x[:2]), "Audit legacy receipts",
                             "Legacy recorded_at/unverified receipts are not new publication work.",
                             [command("sync", "audit", "--clear-missing", "--readme", profile, "--interview-dir", ROOT / "docs/interview", "--notebook-file", "<read-back status note>"),
                              "Supply the current destination texts; --clear-missing removes only absent unverified receipts and preserves every other receipt."],
                             "No unverified destination receipts remain; absent destinations become ordinary records actions."))
    history = history_action(at)
    if history:
        result.append(history)
    waits = {("records", "profile"), ("records", "history"), ("repair", "history-cache")}
    if not any((entry["kind"], entry["target"]) in waits for entry in result):
        header = profile_header_action(profile)
        if header:
            result.append(header)
    return result


def wave_task(item, role, facts, feedback=()):
    target = item_target(item)
    repo = canonical(item.meta["repo"])
    task = {"key": identifier(role, target, [facts, [e["key"] for e in feedback]]),
            "role": role, "item": target, "repo": repo, "lock_key": repo.rsplit("/", 1)[-1].lower()}
    if role == "follow":
        details = [{**detail(e), "text": f"CONFLICT {e['url']}: {followup.CONFLICT_REPAIR}"}
                   if e["kind"] == "conflict" else e["detail"] for e in feedback]
        task["note"] = json.dumps(details, ensure_ascii=False) + "\nSave event_keys in follow.json: " + json.dumps([e["key"] for e in feedback])
    return task


def item_actions(item, bucket, entry, feedback):
    target = item_target(item)
    if bucket in {"needs_value_review", "needs_implementation", "proposal_ready"}:
        facts = {"demand_sha256": verdicts.demand_sha256(item), "value_review": object_data(item.file("value-review.json"))}
        if bucket == "needs_value_review":
            return [], wave_task(item, "value-review", facts)
        if bucket == "needs_implementation":
            with evidence_file(item.file("verify.md")):
                facts["self_review"] = verdicts.read_self_review(item) or {}
            task = wave_task(item, "implement", facts)
            verdict = facts["self_review"].get("verdict")
            if verdict != "ship":
                task["note"] = f"verify.md self-review verdict: {verdict or 'missing'}. Finish the implementation and self-review before handoff."
            return [], task
        with evidence_file(item.file("proposal.md")):
            facts["proposal_sha256"] = hashlib.sha256(item.file("proposal.md").read_bytes()).hexdigest()
        proposal = action("propose", target, facts, f"Post the proposal for {item.dir.name}",
                          "value-review.json verdict: " + facts["value_review"]["verdict"],
                          [f"Read {target}/proposal.md against {target}/demand.json and the cited sources; correct it yourself before approval.",
                           command("approve-proposal", target, "--note", CONFIG["standing_approval_note"]), command("propose", target)],
                          "The item leaves proposal_ready after the posted proposal and waiting status are recorded, or it is parked with a reason.")
        return [proposal], None
    facts = fingerprint(item)
    item.meta.setdefault("review_budget_minutes", CONFIG.get("limits", {}).get("review_budget_minutes", 120))
    with evidence_file(item.file(review.timeline.FILE_NAME)):
        history = read_json(item.file(review.timeline.FILE_NAME), [])
        if not isinstance(history, list) or not all(isinstance(event, dict) for event in history):
            raise ValueError("expected a JSON array of timeline events")
        takeover = review.needs_takeover(item)
    if takeover:
        brief = command("brief", "takeover", "--item", target)
        return [action("takeover", target, [facts, review.rounds(item)], f"Take over {item.dir.name}",
                       "A correction round still needs fixes, or the latest review is incomplete or over budget.", [brief, parking(item)],
                       review_done(item, "The item is parked, or a newer completed review covers its current head."), brief=brief)], None
    decision = facts["coordinator"] or {}
    head = facts["head"]
    decision_current = bool(head and verdicts.bound(item, head, decision.get("head")))
    replied = False
    prepared = False
    if feedback:
        with evidence_file(item.file("follow.json")):
            replied = follow_replied(item, feedback)
            prepared = not replied and follow_prepared(item, feedback)
    result = []
    if bucket == "needs_coordinator_review" and not (decision_current and decision.get("verdict") in {"fix-needed", "drop"}):
        result.append(review_action(item, "; ".join(entry.get("reasons", []))))
    elif bucket == "ship_ready":
        result.append(action("ship", target, facts, f"Ship {item.dir.name}", "Plan lists this item as ship_ready.",
                             [command("ship", target, "--dry-run"), command("ship", target)], "The item has a pr_url and leaves the plan."))
    elif prepared:
        reviewed = facts["review"]
        decision_current = bool(decision_current and item.file(decision["source"]).stat().st_mtime_ns
                                >= item.file(reviewed["source"]).stat().st_mtime_ns)
        if reviewed["verdict"] != "ship":
            result.append(action("held", target, facts, f"Resolve {item.dir.name}", "Independent verdict: " + reviewed["verdict"],
                                 [parking(item)], review_done(item, "Park the item or obtain a new current-head review.")))
        elif decision_current and decision.get("verdict") == "ship" and (item.schema < 2 or decision.get("pr_sha256") == verdicts.pr_text_sha256(item)):
            result.append(follow_ship(item))
        elif not decision_current or decision.get("verdict") not in {"fix-needed", "drop"}:
            result.append(review_action(item, "The open-PR feedback is prepared; personally review before submit or body/reply updates."))
        else:
            result.append(action("held", target, facts, f"Resolve {item.dir.name}", "Coordinator verdict: " + decision["verdict"],
                                 [parking(item)], review_done(item, "Park the item or obtain a new current-head review.", "coordinator")))
    elif bucket == "held" or (bucket == "needs_coordinator_review" and decision_current):
        reasons = entry.get("reasons", []) or ["Coordinator verdict: " + str(decision.get("verdict"))]
        result.append(action("held", target, [facts, reasons], f"Resolve the hold on {item.dir.name}", "; ".join(reasons),
                             ["Resolve exactly the listed reasons with real evidence; do not fabricate impact or rewrite historical review heads.", parking(item)],
                             "The item leaves held, or is parked with a reason."))
    role = None
    if not prepared:
        if bucket == "fix_round" and review.rounds(item) == 0:
            role = "fix"
        elif bucket == "stale_reviews":
            role = "review"
        elif bucket == "unreviewed":
            with evidence_file(item.file("verify.md")):
                if (verdicts.read_self_review(item) or {}).get("verdict") == "ship":
                    role = "review"
        if feedback and not replied:
            role = "follow"
    task = wave_task(item, role, facts, feedback) if role else None
    return result, task


def stalled_action(item, at):
    """Use saved work dates and git history; a fresh checkout's mtimes are not progress."""
    dates = [parse_iso(item.meta[key]) for key in ("created_at", "updated_at") if item.meta.get(key)]
    if not dates or at - max(dates) < ITEM_STALL:
        return None
    target = item_target(item)
    paths = [str(pathlib.Path(target) / name) for name in PROGRESS_FILES]
    committed = run(["git", "-C", str(ROOT), "log", "-1", "--format=%cI", "--", *paths]).stdout.strip()
    if committed:
        dates.append(parse_iso(committed))
    # Only changed evidence may use mtimes, never files merely materialized by checkout.
    for name, _ in changed_paths(paths):
        path = ROOT / name
        if path.is_file():
            dates.append(dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc))
    if at - max(dates) < ITEM_STALL:
        return None
    progress = iso(max(dates))
    return action("stalled-item", target, [progress, item.meta.get("status")], f"Triage stalled item {item.dir.name}",
                  f"No recorded work since {progress} (at least {ITEM_STALL.days} days); this unfinished item reserves its repository.",
                  [f"Read {target}/item.json and its saved demand, implementation and review evidence. Check whether the work is still wanted and unclaimed.",
                   "Choose one disposition: finish the missing stage with real evidence, park with an evidence-backed reason and reopening condition, "
                   "or set status to dropped in item.json with the reason. Do not bypass independent review or publish during triage.",
                   "If finishing needs a missing clone, follow docs/repairs.md#clone-recovery; do not restore code merely to park or drop the item.",
                   parking(item), command("next")],
                  "New work evidence advances the item, or it is explicitly parked, blocked or dropped with a reason. Triage cannot be acknowledged away.")


def scout_history(acks):
    """New empty-scout acknowledgements retain repo identity; old hashes are not reversible."""
    history = {}
    for key, ack in acks.items():
        if key.startswith("scout-") and ack.get("repo"):
            repo = canonical(ack["repo"]).lower()
            stamp = parse_iso(ack["at"])
            if repo not in history or stamp > history[repo]:
                history[repo] = stamp
    return history


def upstream_activity(rows, events):
    """Use saved source-keyed feedback and outcome dates, not repeated poll timestamps."""
    activity = {}
    for entry in rows:
        stamps = [entry[key] for key in ("merged_at", "closed_at") if entry.get(key)]
        if entry.get("repo") and stamps:
            repo = canonical(entry["repo"]).lower()
            stamp = max(map(parse_iso, stamps))
            activity[repo] = max(activity.get(repo, stamp), stamp)
    for entry in events:
        if entry["kind"] not in {"maintainer-comment", "maintainer-review", "community-comment", "community-review"}:
            continue
        if entry.get("repo"):
            repo, stamp = canonical(entry["repo"]).lower(), parse_iso(entry["at"])
            activity[repo] = max(activity.get(repo, stamp), stamp)
    return activity


def release_action(active):
    if not clean.checkouts():
        return None
    reports = clones.release_idle(active, dry_run=True)["reports"]
    ready = [row for row in reports if row.get("would_release")]
    if not ready:
        return None
    total = sum(clean.size(pathlib.Path(row["checkout"])) for row in ready)
    why = "Idle checkouts: " + ", ".join(row["checkout"] for row in ready) + f"; total {total} bytes ({total / 2**30:.2f} GiB)."
    refused = [row for row in reports if not row.get("would_release")]
    if refused:
        why += " Refused: " + "; ".join(row["checkout"] + ": " + ", ".join(row["why"]) for row in refused)
    return action("release", "idle-clones", reports, "Release idle clones", why,
                  [command("clone", "release", "--idle"),
                   "Run bin/factory next and complete its commit action for all changed release metadata and other shift records before closing the shift."],
                  "No releasable idle checkout remains; commit any changed item.json records in this closeout, not a later cleanup pass.")


def actions(observe=True, include_release=True):
    at = now()
    acks = read_acks()
    push = push_action()
    result = [push] if push else []
    free = shutil.disk_usage(pathlib.Path.home()).free
    if free < DISK_FLOOR_GIB * 1024 ** 3:
        utc = at.astimezone(dt.timezone.utc)
        half_day = f"{utc:%Y-%m-%d}-{'am' if utc.hour < 12 else 'pm'}"
        result.append(action("disk", "home-volume", {"minimum_gib": DISK_FLOOR_GIB, "half_day": half_day}, "Recover disk space",
                             f"The home volume has {free / 1024 ** 3:.1f} GiB free; installs, builds and tests need at least {DISK_FLOOR_GIB} GiB.",
                             [command("clone", "release", "--idle"),
                              "Read docs/repairs.md#disk-space and clean only inactive, regenerable factory resources.",
                              "Still below the floor after those steps: record the blocker once in docs/owner-actions.md and then acknowledge with the free space left "
                              "and what holds the rest. The acknowledgement lasts for this UTC half-day; until space returns, do only work that needs no install."],
                             f"The home volume has at least {DISK_FLOOR_GIB} GiB free, or the shortage is acknowledged for this half-day."))
    state = plan.run()
    rows = ledger.load()
    by_url = {r["url"]: r for r in rows}
    events, source = observations(observe)
    activity = upstream_activity(rows, events)
    events = [e for e in events if current_event(e, by_url)]
    unobserved_repos = {canonical(e["url"].removeprefix("https://github.com/")).lower()
                        for e in events if merger_error(e)}
    for bucket in ("missing_clone", "errors"):
        for entry in state.get(bucket, []):
            target = entry["item"]
            commands = ["Read docs/repairs.md#clone-recovery and inspect the item path, clone and branch before changing anything."]
            if entry.get("clone"):
                commands += [command("clone-heal", entry["clone"], "--dry-run"), command("clone-heal", entry["clone"]),
                             "No open PR and nothing awaited from a maintainer: do not clone again; park it with "
                             f"bin/factory park {target} --reason '<evidence-backed reason>'."]
            result.append(action("repair", target, entry, f"Repair {target}", entry.get("error") or f"Clone missing: {entry.get('clone')}",
                                 commands, "The item leaves missing_clone/errors, or is explicitly parked with a reason."))
    broken = {(ROOT / e["item"]).resolve() for e in state.get("errors", [])}
    loaded = {}

    def load_item(value, row_url=None):
        key = str(value)
        if key not in loaded:
            loaded[key] = None
            try:
                path = local_path(value)
                if path not in broken:
                    resolved_key = str(path)
                    if resolved_key not in loaded or resolved_key == key:
                        loaded[resolved_key] = None
                        loaded[resolved_key] = local_item(path)
                    loaded[key] = loaded[resolved_key]
            except ITEM_ERRORS as error:
                label = f"ledger row {row_url} ({value})" if row_url else value
                result.append(repair_action(label, error))
        return loaded[key]

    for row in rows:
        if row.get("item"):
            load_item(row["item"], row["url"])
    for row in ledger.missing_lessons(rows):
        url = row["url"]
        result.append(action("lesson", url, [row.get("status"), row.get("silent"), row.get("lesson"), row.get("closed_at")],
                             "Record the missing lesson", "New PRs are blocked until closed or silent PRs have valid lesson records. "
                             "A lesson from before this PR closed does not cover its closing feedback.",
                             [command("gh", "--", "pr", "view", url, "--json", "state,comments,reviews"),
                              command("lesson", "add", "--pr", url, "--title", "<what was learned>", "--rule", "<evidence-backed rule>",
                                      "--scope", row["repo"], "--source", url)],
                             "The PR references a valid lesson covering its current closing or silence evidence; then retry shipping."))
    owner_path = ROOT / "docs/owner-actions.md"
    owner_text = owner_path.read_text() if owner_path.exists() else ""
    feedback, ci = {}, {}
    for event in events:
        kind, data, url = event["kind"], detail(event), event["url"]
        if kind in {"cla", "awaiting-cla"}:
            if url not in owner_text:
                result.append(action("owner-action", url, event["key"], "Record the owner's CLA action", source + " " + data.get("text", ""),
                                     [f"Add {url} and the concrete owner-only step to docs/owner-actions.md with this step: {data.get('owner_action', 'Resolve the CLA check.')}",
                                      "Notify the owner once, then continue other work; do not sign on their behalf."], "docs/owner-actions.md names this PR URL."))
        elif kind in {"ci-failing", "pre-existing-failures"}:
            ci.setdefault((url, event_head(event)), []).append(event)
        elif kind in {"stale-pr-head", "stale-head", "issue-taken", "issue-confirmed"}:
            name = "stale-head" if kind == "stale-pr-head" else kind
            target = event.get("item") or by_url.get(url, {}).get("item")
            if target:
                item = load_item(target, url)
                if item is None or inactive(item, by_url):
                    continue
                target = item_target(item)
            cmd = [command("gh", "--", "pr" if name == "stale-head" else "issue", "view", url, "--json", "state,headRefOid" if name == "stale-head" else "state,comments")]
            section = "stale-pr-head" if name == "stale-head" else "waiting-issues"
            cmd.append(f"Read docs/repairs.md#{section} and verify the live evidence before acting. " + data.get("text", ""))
            if name == "stale-head" and target:
                cmd.append("After current-head review and approval: " + command("submit", target, "--dry-run"))
                cmd.append(command("submit", target))
                endpoint = f"repos/{event['repo']}/pulls/{event['number']}"
                for state_value, condition in (("closed", "Only if the OPEN PR still displays an older head than the verified fork branch: "),
                                               ("open", "Immediately after that close succeeds, reopen the same PR: ")):
                    cmd.append(condition + command("gh", "--item", target, "--", "api", "-X", "PATCH", endpoint, "-f", "state=" + state_value))
                cmd.append(command("gh", "--", "pr", "view", url, "--json", "state,headRefOid"))
            done = "Run bin/factory next after the repair/decision; acknowledge with live evidence only if this ID is still pending."
            if name == "issue-taken":
                cmd.append("If the competing work solves this issue, mark the item dropped with its URL and reason; this settles the action without ack. "
                           "Otherwise acknowledge only the still-pending ID with evidence that the reference is unrelated.")
            if target and name in {"stale-head", "issue-confirmed"}:
                done = review_done(item, review_done(item, done), "coordinator")
            result.append(action(name, url, [event["key"], data, by_url.get(url, {}).get("head_sha")], name.replace("-", " ").capitalize(),
                                 source + " " + data.get("text", ""), cmd, done))
        elif kind in FEEDBACK:
            target = event.get("item") or by_url.get(url, {}).get("item")
            if target:
                item = load_item(target, url)
                if item is not None:
                    feedback.setdefault(item_target(item), []).append(event)
        elif kind == "followup-error":
            entry = action("repair", url or event.get("item") or "followup", event["key"], "Repair the failed observation",
                           source + " " + data.get("text", ""), ["Fix the reported read failure without changing security settings.", command("next", "--refresh")],
                           "A newer successful observation supersedes this error.")
            if merger_error(event):
                entry["observation"] = True
                entry["commands"].append("If the read still fails, acknowledge this ID with the failure evidence to defer it for twelve hours; continue other repositories.")
                entry["done_when"] += " Deferral does not clear the warning or permit code work or scouting in this repository."
            result.append(entry)
    for (url, head), failures in sorted(ci.items(), key=lambda pair: str(pair[0])):
        jobs = {json.dumps(j, sort_keys=True): j for e in failures for j in detail(e).get("jobs", [])}
        jobs = list(jobs.values()) or [{"name": n} for n in (by_url.get(url, {}).get("checks") or {}).get("failing", [])]
        cmds = []
        for job in jobs:
            if job.get("run_id"):
                cmds.append(command("gh", "--", "run", "view", job["run_id"], "-R", failures[0]["repo"], "--log-failed"))
            else:
                cmds.append(f"Read the failing job log for {job['name']} at {job.get('url') or url}")
        cmds.append("Compare each job/version/error with base; docs/repairs.md#ci-triage lists leads, not exemptions. Fix ours on the same branch through the reviewed submit path.")
        names = sorted({j["name"] for j in jobs})
        result.append(action("ci-triage", url, {"head": head or by_url.get(url, {}).get("head_sha"), "jobs": jobs, "events": sorted(e["key"] for e in failures)},
                             "Triage failing CI", source + " Jobs: " + ", ".join(names), cmds,
                             "Ack with one line per job: JOB: ours|external — concrete log/base evidence. Jobs: " + json.dumps(names)))
    entries, items = {}, {}
    for bucket in ("needs_coordinator_review", "ship_ready", "fix_round", "stale_reviews", "held", "unreviewed",
                   "needs_value_review", "needs_implementation", "proposal_ready"):
        for entry in state.get(bucket, []):
            item = load_item(entry["item"])
            if item is not None and not inactive(item, by_url) and not item.meta.get("pr_url"):
                target = item_target(item)
                entries[target] = (bucket, entry)
                items[target] = item
    for target in feedback:
        item = load_item(target)
        if item is not None and not inactive(item, by_url):
            items[target] = item
    lock = shiftlock.live(shiftlock.load(ROOT / shiftlock.STATE), at)
    shift = lock.get("generation") if lock else at.astimezone(ZONE).date().isoformat()
    start = parse_iso(lock["acquired_at"]) if lock else dt.datetime.combine(at.astimezone(ZONE).date(), dt.time(), ZONE)
    unfinished, hunted, stalled = set(unobserved_repos), set(), set()
    last_empty = scout_history(acks)
    last_scout = dict(last_empty)
    empty_this_shift = {repo for repo, stamp in last_empty.items() if stamp >= start}
    productive = set()
    hunted.update(empty_this_shift)
    missing_clones = {(ROOT / entry["item"]).resolve() for entry in state.get("missing_clone", [])}
    for path in sorted(path_from_config("runs_dir").rglob("item.json")):
        item = load_item(path.parent)
        if item is None:
            continue
        repo, target = canonical(item.meta["repo"]).lower(), item_target(item)
        unfinished_item = not item.meta.get("pr_url") and not inactive(item, by_url)
        if unfinished_item:
            unfinished.add(repo)
        try:
            with evidence_file(path):
                created = item.meta.get("created_at")
                if created:
                    created = parse_iso(created)
                    if repo not in last_scout or created > last_scout[repo]:
                        last_scout[repo] = created
                    if repo in last_empty and created >= last_empty[repo]:
                        del last_empty[repo]
                    if created >= start:
                        hunted.add(repo)
                        productive.add(repo)
            if unfinished_item and repo not in unobserved_repos:
                triage = stalled_action(item, at)
                if triage:
                    if item.dir in missing_clones:
                        result = [entry for entry in result if not (
                            entry["kind"] == "repair" and (ROOT / entry["target"]).resolve() == item.dir)]
                    result.append(triage)
                    stalled.add(target)
        except (CommandError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            result.append(repair_action(target, error))
            unfinished.add(repo)
            broken.add(item.dir)
    tasks, blocked = [], []
    for target, item in items.items():
        if canonical(item.meta["repo"]).lower() in unobserved_repos or target in stalled or item.dir in broken:
            continue
        bucket, entry = entries.get(target, (None, {}))
        try:
            blocker = follow_blocker(item, feedback.get(target, []))
            if blocker:
                blocked.append(blocker)
                continue
            pending, task = item_actions(item, bucket, entry, feedback.get(target, []))
        except ITEM_ERRORS as error:
            result.append(repair_action(target, error))
            unfinished.add(canonical(item.meta["repo"]).lower())
            continue
        result += pending
        if task:
            tasks.append(task)
    slots = {canonical(s["repo"]).lower(): s for s in state.get("repo_slots", [])}
    limits = CONFIG.get("limits", {})
    cutoff = at - dt.timedelta(days=limits.get("open_pr_window_days", 180))
    grouped = plan.by_repo([row for row in rows if row.get("status") in ledger.DONE
                            or not row.get("submitted_at") or parse_iso(row["submitted_at"]) >= cutoff])
    proven = {repo for repo, group in grouped.items() if any(row.get("status") == "merged" for row in group)}
    exploring = sum(row.get("status") not in ledger.DONE for repo, group in grouped.items()
                    if repo not in proven and not repo.startswith(CONFIG["github_user"].lower() + "/") for row in group)
    limit = limits.get("max_hunts_per_shift", 3)
    active = {lane: lanes.order(lane) for lane in ("depth", "explore")}
    candidates = {lane: [] for lane in active}
    depth_cleared = set()
    for lane, repos in active.items():
        for repo in repos:
            key = canonical(repo).lower()
            task_id = identifier("scout", repo, {"shift": shift})
            if task_id in acks:
                hunted.add(key)
                empty_this_shift.add(key)
            slot = slots.get(key)
            if slot is None:
                slot = plan.slot(grouped[key]) if key in grouped else {"free": limits["max_open_prs_per_repo_before_first_merge"]}
            free = slot.get("free", 0)
            if key not in proven:
                free = min(free, max(0, limits["max_open_prs_total"] - exploring))
            no_slot = free <= 0 or slot.get("stopped")
            empty_at = last_empty.get(key)
            cooling = (empty_at and at < empty_at + SCOUT_COOLDOWN and activity.get(key, empty_at) <= empty_at)
            if lane == "depth" and (no_slot or cooling or (key in empty_this_shift and key not in productive)):
                depth_cleared.add(key)
            if no_slot or cooling or key in unfinished or key in hunted:
                continue
            candidates[lane].append({"key": task_id, "role": "scout", "repo": canonical(repo), "lock_key": key.rsplit("/", 1)[-1]})
        # Stable sorting preserves lane order for ties, including repositories never scouted.
        candidates[lane].sort(key=lambda task: last_scout.get(task["repo"].lower(), dt.datetime.min.replace(tzinfo=dt.timezone.utc)))
    depth = {canonical(repo).lower() for repo in active["depth"]}
    exploration = {canonical(repo).lower() for repo in active["explore"]}
    scouts = candidates["depth"]
    if depth <= depth_cleared and not (exploration & hunted):
        scouts += candidates["explore"][:1]
    tasks += scouts[:max(0, limit - len(hunted))]
    if tasks:
        args = {"factory": str(ROOT), "concurrency": CONFIG.get("limits", {}).get("concurrency", 3), "tasks": tasks}
        renew = command("lock", "renew", "--owner", lock["owner"] if lock else "<owner>", "--generation", lock["generation"] if lock else "<generation from shift start>", "--ttl-hours", 8)
        cmds = [renew, f"Call Workflow with scriptPath {ROOT / 'schedule/oss-wave.js'} and the following args:", json.dumps(args, ensure_ascii=False)]
        for target in sorted({t["item"] for t in tasks if t.get("item")}):
            cmds.append(command("provenance", "--item", target, "--transcripts", "<transcript dir>"))
        if any(t["role"] == "scout" for t in tasks):
            cmds.append("For each item created by a scout, run bin/factory provenance --item <returned item dir> --transcripts <transcript dir>")
            for task in tasks:
                if task["role"] == "scout":
                    cmds.append("Only if this scout found no candidate: " + command("ack", task["key"], "--note", "<examined sources and why no candidate qualifies>"))
        cmds.append(command("next"))
        done = "Tasks advance from saved demand, value decisions, implementation and reviews; acknowledge only a scout with no candidate."
        for task in tasks:
            if task.get("item"):
                part = "value" if task["role"] == "value-review" else "review"
                done = review_done(items[task["item"]], done, part)
        if any(task["role"] == "scout" for task in tasks):
            done += (" For each new schema-2 item, validate demand; build/propose candidates also need a completed value review."
                     " Only a build decision continues to implementation and bin/factory review check --item <returned item dir> review")
        result.append(action("wave", "shift", args, "Run the pending work wave", source + " Tasks are selected from current evidence, lane eligibility and available slots.", cmds, done))
    result += record_actions(rows, at, load_item)
    problems = lessons.lint()
    if problems:
        result.append(action("route-lessons", "lessons", problems, "Route lessons", "\n".join(problems[:5]),
                             [command("lessons", "lint")], "bin/factory lessons lint reports no problems."))
    try:
        commit = commit_action()
        if commit:
            result.append(commit)
    except ITEM_ERRORS as error:
        result.append(repair_action("changed files", error))
    if include_release:
        try:
            release = release_action(code_items(result))
            if release:
                result.append(release)
        except ITEM_ERRORS as error:
            result.append(repair_action("idle clones", error))
    today = at.astimezone(ZONE).date()
    dates = [d for d in retro_dates() if d <= today]
    if today.weekday() == 0 and not any((today - d).days <= 6 for d in dates):
        since = max(dates).isoformat() if dates else (today - dt.timedelta(days=7)).isoformat()
        result.append(action("measure", today.isoformat(), since, "Measure the week", "Monday in UTC+8; no retrospective page in the last six days.",
                             [command("influence", "--record"), command("sync", "retro", "--since", since, "--write", f"workspace/retro/{today}"),
                              f"Write docs/retro/{today}.md with separate submitted-since and merged-since populations; review lines/oss/lanes.json against comparable two-week evidence."],
                             "A retrospective page is dated within the last six days."))
    sentence = report_line(rows, at)
    why = "Ledger totals use UTC+8 calendar days; owner actions count only open entries."
    blocked.sort(key=lambda entry: entry["item"])
    for entry in blocked:
        why += (f"\nNot re-dispatched: {entry['item']} — " + "; ".join(entry["blockers"])
                + f" Reopen when: {entry['reopen_when']} New feedback or a changed head also permits reassessment; "
                "refreshing the same observation does not. Continue unrelated work; do not retry this action. "
                "Legal agreements, account/settings work and required owner-authored prose go in docs/owner-actions.md; notify once. "
                "Do not duplicate owner actions or escalate ordinary environmental blockers.")
    result.append(action("report", today.isoformat(), [sentence, blocked] if blocked else sentence, sentence, why,
                         ["Relay this sentence to the owner unchanged: " + sentence], "Acknowledge this ID after relaying the sentence."))
    def acknowledged(entry):
        ack = acks.get(entry["id"])
        if not ack:
            return False
        if entry.get("observation"):
            return at < parse_iso(ack["at"]) + dt.timedelta(hours=12)
        return entry["kind"] in ACKABLE

    unique = {entry["id"]: entry for entry in result if not acknowledged(entry)}
    # Close-of-session records: refresh the saved history, then publish the profile once from it.
    history_pending = any(entry["kind"] == "records" and entry["target"] == "history" for entry in unique.values())
    closing = {"history": 1, "profile": 2, "profile-header": 3} if history_pending else {"profile-header": 3}

    def order(entry):
        if entry["kind"] == "records" and entry["target"] in closing:
            return ORDER["report"] - 1, closing[entry["target"]]
        return ORDER[entry["kind"]], 0

    return sorted(unique.values(), key=order)


def wave_tasks(entry):
    if entry["kind"] == "wave":
        for text in entry["commands"]:
            if text.startswith('{"factory":'):
                return json.loads(text)["tasks"]
    return []


def code_items(pending=None):
    """Resolved item directories whose pending work needs code, using saved observations only."""
    if pending is None:
        pending = actions(observe=False, include_release=False)
    result = set()
    for entry in pending:
        if entry["kind"] in {"coordinator-review", "ship", "takeover"}:
            result.add(local_path(entry["target"]))
        for task in wave_tasks(entry):
            if task["role"] in {"implement", "review", "fix", "follow", "takeover"} and task.get("item"):
                result.add(local_path(task["item"]))
    return result


@contextlib.contextmanager
def guarded():
    with (ROOT / GUARD).open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def acknowledge(action_id, note):
    if not note.strip():
        raise ValueError("--note must explain the observed result")
    with guarded():
        pending = actions(observe=False)
        entry = next((a for a in pending if a["id"] == action_id), None)
        scouts = {}
        for pending_action in pending:
            scouts.update((task["key"], task) for task in wave_tasks(pending_action) if task["role"] == "scout")
        if entry is None and action_id not in scouts:
            raise ValueError("ID is no longer pending; run bin/factory next and use the current ID")
        if entry and entry["kind"] not in ACKABLE and not entry.get("observation"):
            raise ValueError("This action closes through observed state, not ack; satisfy done_when or park the item")
        if entry and entry["kind"] == "disk" and not re.search(r"\d+(?:\.\d+)?\s*GiB", note):
            raise ValueError("State the free space left and what holds the rest, for example "
                             "'3.2 GiB free after clean --prune; the remainder belongs to other sessions'")
        if entry and entry["kind"] == "ci-triage":
            names = json.loads(entry["done_when"].split("Jobs: ", 1)[1])
            if not names:
                raise ValueError("Failing jobs are missing; refresh the checks before acknowledging CI")
            for name in names:
                pattern = r"(?:^|\n)\s*" + re.escape(name) + r"\s*:\s*(ours|external)\b\s*[-—:]\s*\S.+"
                if not re.search(pattern, note, re.I):
                    raise ValueError(f"Add a line '{name}: ours|external — concrete log/base evidence' before acknowledging CI")
        acks = read_acks()
        ids = [action_id]
        if entry and entry.get("observation"):
            # The printed refresh can add another failure before this acknowledgement.
            ids = [a["id"] for a in pending if a.get("observation") and a["target"] == entry["target"]]
        for pending_id in ids:
            acks[pending_id] = {"at": iso(now()), "note": note.strip()}
            if pending_id in scouts:
                acks[pending_id]["repo"] = scouts[pending_id]["repo"]
        write_json(ROOT / ACKS, acks)


def print_action(entry):
    print(entry["title"])
    print("Why: " + entry["why"])
    for index, text in enumerate(entry["commands"], 1):
        print(f"{index}. {text}")
    print("Done when: " + entry["done_when"])
    print("ID: " + entry["id"])
    if entry["kind"] in ACKABLE or entry.get("observation"):
        prefix = "" if entry["kind"] == "report" else "Only if this ID is still pending after bin/factory next: "
        print(prefix + command("ack", entry["id"], "--note", "<observed result and evidence>"))


def cmd_next(args):
    def progress(message):
        print("refresh: " + message, file=sys.stderr, flush=True)

    try:
        if args.refresh:
            progress("checking PR observations")
            followup.run(progress=progress)
            progress("checking waiting issues")
            followup.waiting_issues(progress=progress)
            progress("projecting next actions")
        pending = actions()
        if args.json:
            if args.all:
                result = pending
            elif pending:
                result = pending[0]
            else:
                result = None
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif not pending:
            print("idle")
        else:
            for entry in pending if args.all else pending[:1]:
                print_action(entry)
    except (OSError, ValueError) as error:
        raise SystemExit(f"next refused: {error}") from None


def cmd_ack(args):
    try:
        acknowledge(args.id, args.note)
    except (OSError, ValueError) as error:
        raise SystemExit(f"ack refused: {error}") from None
    print("Acknowledged " + args.id)


def cmd_shift(args):
    try:
        with guarded():
            if args.shift_command == "start":
                status = shiftlock.run("status")
                if not status["ok"]:
                    print(status["reason"])
                    return 1
                if not status["free"]:
                    print(f"Shift held by {status['holder']['owner']}; continue that shift with bin/factory next instead of starting another shift.")
                    return 1
                baseline, _ = commit_changes(include_runs=True)
                result = shiftlock.run("acquire", args.owner, ttl_hours=args.ttl_hours, commit_baseline=baseline)
            else:
                result = shiftlock.run("release", args.owner)
        if not result["ok"]:
            print(result["reason"])
            return 1
        if args.shift_command == "end":
            print(report_line())
        else:
            print("Generation: " + result["holder"]["generation"])
            pending = actions()
            if pending:
                print_action(pending[0])
            else:
                print("idle")
    except (OSError, ValueError, CommandError) as error:
        print(f"shift refused: {error}; fix the cause before starting another shift")
        return 1
    return 0


def register(sub):
    parser = sub.add_parser("next", help="show pending work from saved state; only --refresh sweeps GitHub")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="explicitly run followup and waiting-issue observations")
    parser.set_defaults(fn=cmd_next)
    parser = sub.add_parser("ack", help="close an unobservable result for the current action ID only")
    parser.add_argument("id")
    parser.add_argument("--note", required=True)
    parser.set_defaults(fn=cmd_ack)
    parser = sub.add_parser("shift", help="start or end a session shift")
    commands = parser.add_subparsers(dest="shift_command", required=True)
    for name in ("start", "end"):
        part = commands.add_parser(name)
        part.add_argument("--owner", required=True)
        if name == "start":
            part.add_argument("--ttl-hours", type=float, default=4)
        part.set_defaults(fn=cmd_shift)
