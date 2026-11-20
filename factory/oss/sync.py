"""Derive contribution records locally and track a separate receipt for each destination."""
import collections
import datetime as dt
import difflib
import fcntl
import hashlib
import json
import pathlib
import re
import shlex
import sys
from urllib.parse import quote

from factory.core import clones, ledger
from factory.core.repos import canonical
from factory.oss import profile_readme, retro
from factory.util import CONFIG, ROOT, clone_dir, iso, now, parse_iso, path_from_config

DESTINATIONS = ("profile", "teach", "notebook")
INDEX_START = "<!-- factory:index:start -->"
INDEX_END = "<!-- factory:index:end -->"
REFERENCE = re.compile(r"(?P<url>\b[a-z][a-z0-9+.-]*://[^\s<>]+)"
                       r"|(?P<repo>(?<![\w./-])[\w.-]+/[\w.-]+(?![\w./-]))"
                       r"|\#(?P<number>\d+)(?![A-Za-z0-9_])", re.I)


def records(row):
    if "records" not in row and row.get("recorded_at"):
        return {dest: {"unverified": True, "at": row["recorded_at"]} for dest in DESTINATIONS}
    return dict(row.get("records") or {})


def real_receipt(receipt):
    return (isinstance(receipt, dict) and not receipt.get("unverified") and "deferred" not in receipt
            and bool(receipt.get("at")) and bool(receipt.get("ref"))
            and isinstance(receipt.get("sha256"), str)
            and bool(re.fullmatch(r"[0-9a-fA-F]{64}", receipt["sha256"])))


def unverified(receipt):
    return isinstance(receipt, dict) and bool(receipt.get("unverified"))


def complete(row):
    return all(real_receipt(records(row).get(dest)) for dest in DESTINATIONS)


def status():
    rows = [r for r in ledger.load() if r["status"] == "merged"]
    return {dest: [r["url"] for r in sorted(rows, key=lambda r: unverified(records(r).get(dest)))
                   if not real_receipt(records(r).get(dest))] for dest in DESTINATIONS}


def destination_texts(value):
    path = pathlib.Path(value).expanduser()
    if not path.exists():
        raise ValueError(f"{path} does not exist; provide a readable destination or omit it")
    if path.is_dir():
        paths = sorted(p for p in path.rglob("*") if p.is_file() and p.suffix in (".md", ".txt"))
    else:
        paths = [path]
    result = []
    for source in paths:
        content = source.read_bytes()
        result.append((source, content.decode("utf-8"), hashlib.sha256(content).hexdigest()))
    return result


def mark(destination, url=None, *, file=None, ref=None, deferred=None):
    if destination not in DESTINATIONS:
        raise ValueError("choose profile, teach or notebook")
    if (file is None) == (deferred is None):
        raise ValueError("supply --file with the read-back destination text, or --deferred with a Private notebook retry reason, not both")
    if deferred is not None and (destination != "notebook" or not url or not deferred.strip() or ref is not None):
        raise ValueError("use notebook <pr-url> --deferred '<nonempty retry reason>', without --ref or --file")
    texts = destination_texts(file) if file is not None else []
    result = {"marked": [], "missing": []}
    with path_from_config("ledger_file").with_suffix(".records.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        rows = ledger.load()
        merged_counts = profile_readme.merged_counts(rows)
        if url is not None:
            row = ledger.find(rows, url)
            if row is None:
                raise ValueError(f"{url} is not in the ledger; add the PR before marking its records")
            rows = [row]
        else:
            rows = [row for row in rows if row["status"] == "merged" and not real_receipt(records(row).get(destination))]
        for row in rows:
            receipt = {"at": iso()}
            if deferred is not None:
                receipt["deferred"] = deferred
            else:
                found = next(((source, digest) for source, text, digest in texts
                              if destination_mentioned(text, row, destination, merged_counts)), None)
                if found is None:
                    if url is not None:
                        raise ValueError(f"{file} does not mention {url}; pass --file with the destination text that records this PR")
                    result["missing"].append(row["url"])
                    continue
                source, digest = found
                receipt.update(ref=ref or source.name, sha256=digest)
            updated = records(row)
            updated[destination] = receipt
            saved = ledger.update_fields(row["url"], records=updated)
            if url is not None:
                return saved
            result["marked"].append({"url": row["url"], **receipt})
    return result


def grouped(rows):
    groups = {}
    for row in rows:
        repo = canonical(row["repo"])
        entry = groups.setdefault(repo.lower(), {"repo": repo, "rows": []})
        entry["rows"].append(row)
    return groups


def counts(rows):
    result = {"merged": 0, "open": 0, "closed": 0}
    for row in rows:
        state = row["status"]
        if state in ("merged", "closed"):
            key = state
        elif state == "abandoned":
            key = "closed"
        else:
            key = "open"
        result[key] += 1
    return result


def title(row):
    item = pathlib.Path(row.get("item") or ".")
    if "runs" in item.parts:
        item = ROOT.joinpath(*item.parts[item.parts.index("runs"):])
    elif not item.is_absolute():
        item = ROOT / item
    path = item / "pr.md"
    if row.get("item") and path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
        if lines:
            return re.sub(r"^#{1,6}\s+", "", lines[0]).strip()
    return row.get("title") or row.get("notes") or ""


def cell(value):
    return str(value or "").replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def interview_files(directory):
    files = {}
    for folder in sorted(directory.iterdir()) if directory.exists() else []:
        if not folder.is_dir() or "__" not in folder.name:
            continue
        key = canonical(folder.name.replace("__", "/", 1)).lower()
        files.setdefault(key, []).extend(p for p in sorted(folder.rglob("*")) if p.is_file() and p.suffix in (".md", ".txt"))
    return files


def render_interview(rows, directory):
    directory = pathlib.Path(directory)
    groups = grouped(rows)
    documents = interview_files(directory)
    index = ["## Ledger index", "", "Counts and contribution tables are generated from the ledger; the dated narratives below are unchanged.", "",
             "| Repo | Merged | Open | Closed | Files |", "| --- | ---: | ---: | ---: | --- |"]
    contributions = ["# Contribution records", "", "Generated by `bin/factory sync interview --write` from the ledger and item PR titles.", ""]
    for key in sorted(groups.keys() | documents.keys()):
        entry = groups.get(key, {"repo": key, "rows": []})
        repo, repo_rows = entry["repo"], entry["rows"]
        count = counts(repo_rows)
        links = [f"[{cell(p.name)}]({quote(p.relative_to(directory).as_posix(), safe='/')})" for p in documents.get(key, [])]
        if repo_rows:
            links.insert(0, f"[Contributions](CONTRIBUTIONS.md#{repo.lower().replace('/', '')})")
        index.append(f"| {repo} | {count['merged']} | {count['open']} | {count['closed']} | {' · '.join(links)} |")
        if not repo_rows:
            continue
        contributions += [f"## {repo}", "", "| Number | Title | Status | Submitted | Merged | Link |",
                          "| ---: | --- | --- | --- | --- | --- |"]
        for row in sorted(repo_rows, key=lambda r: r["number"]):
            contributions.append(f"| #{row['number']} | {cell(title(row))} | {row['status']} | {row.get('submitted_at') or ''} | "
                                 f"{row.get('merged_at') or ''} | [PR]({row['url']}) |")
        contributions.append("")
    return "\n".join(index) + "\n", "\n".join(contributions)


def local_output(path, content, write=False):
    path = pathlib.Path(path)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if write:
        if old != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        print(str(path))
    else:
        print("".join(difflib.unified_diff(old.splitlines(True), content.splitlines(True),
                                         fromfile=str(path), tofile=str(path) + " (generated)")), end="")


def profile(readme=None, write=False):
    repo = f"{CONFIG['github_user']}/{CONFIG['github_user']}"
    checkout = clone_dir(repo)
    default = checkout / "README.md"
    path = pathlib.Path(readme) if readme else default
    if not checkout.exists() and path.resolve() == default.resolve():
        clones.ensure_repo(repo)
    text = profile_readme.read(path)
    rendered = profile_readme.render_readme(text)
    result = profile_readme.check(rendered, CONFIG["github_user"])
    if not result["ok"]:
        raise ValueError("profile check failed; repair the README before writing: " + json.dumps(result))
    local_output(path, rendered, write)


def interview(write=False):
    directory = ROOT / "docs/interview"
    index_path = directory / "README.md"
    text = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    if profile_readme.block(text, INDEX_START, INDEX_END) is None:
        heading_end = text.find("\n") + 1 if text else 0
        text = text[:heading_end] + "\n" + INDEX_START + "\n" + INDEX_END + "\n\n" + text[heading_end:]
    index, contributions = render_interview(ledger.load(), directory)
    local_output(directory / "CONTRIBUTIONS.md", contributions, write)
    local_output(index_path, profile_readme.replace_block(text, index, INDEX_START, INDEX_END), write)


def event_date(row):
    return row.get("merged_at") if row["status"] == "merged" else row.get("closed_at") or row.get("last_check")


def bounded_section(heading, lines, budget):
    full = heading + "\n" + "".join(line + "\n" for line in lines)
    if len(full) <= budget:
        return full
    result = heading + "\n"
    if len(result) + len(f"... {len(lines)} more ledger groups omitted.\n") > budget:
        return ""
    for i, line in enumerate(lines):
        omitted = f"... {len(lines) - i} more ledger groups omitted.\n"
        if len(result) + len(line) + 1 + len(omitted) > budget:
            return result + omitted
        result += line + "\n"
    return result


def notebook(rows, at=None):
    """Keep pending merges intact; fit optional history into the remaining 6,000 characters."""
    at = at or now()
    cutoff = at - dt.timedelta(days=14)
    repo_lines, open_lines, pending_lines = [], [], []
    recent = collections.defaultdict(list)
    events = []
    for entry in sorted(grouped(rows).values(), key=lambda g: g["repo"].lower()):
        repo, repo_rows = entry["repo"], entry["rows"]
        c = counts(repo_rows)
        repo_lines.append(f"{repo}: {c['merged']} merged / {c['open']} open / {c['closed']} closed")
        pending = sorted(r["number"] for r in repo_rows if r["status"] == "merged" and not real_receipt(records(r).get("notebook")))
        if pending:
            pending_lines.append(f"{repo} merged: " + ", ".join(f"#{n}" for n in pending))
        opened = [r for r in repo_rows if r["status"] not in ledger.DONE]
        for row in sorted(opened, key=lambda r: (r.get("submitted_at") or "", r["number"])):
            submitted = row.get("submitted_at")
            age = f"{max(0, (at - parse_iso(submitted)).days)}d" if submitted else "no submitted_at"
            open_lines.append(f"{repo} #{row['number']} ({age})")
        for row in repo_rows:
            if row["status"] not in ("merged", "closed"):
                continue
            date = event_date(row)
            if date and cutoff <= parse_iso(date) <= at:
                day = parse_iso(date).astimezone(dt.timezone.utc).date().isoformat()
                recent[(day, repo, row["status"])].append(row["number"])
            if not real_receipt(records(row).get("notebook")):
                events.append({"date": date, "repo": repo, "number": row["number"], "title": title(row),
                               "url": row["url"], "status": row["status"]})
    recent_lines = [f"{day} {repo} {state}: " + ", ".join(f"#{n}" for n in sorted(numbers))
                    for (day, repo, state), numbers in sorted(recent.items(), reverse=True)]
    c = counts(rows)
    header = (f"# Contribution status\n\nAs of {iso(at)}; ages and dates in UTC.\n"
              f"{len(rows)} PRs: {c['merged']} merged / {c['open']} open / {c['closed']} closed.\n"
              "Close dates use closed_at, or last_check as the observation date when closed_at is absent.\n\n")
    if pending_lines:
        header += "## Pending records\n" + "\n".join(pending_lines) + "\n\n"
    sections = [("## Repositories", repo_lines), ("## Open PRs (age)", open_lines),
                ("## Last 14 days: merges and closes", recent_lines)]
    remaining = max(0, 6000 - len(header) - len(sections))
    rendered = []
    for i, (heading, lines) in enumerate(sections):
        later_size = sum(len(h) + 1 + sum(len(line) + 1 for line in ls) for h, ls in sections[i + 1:])
        budget = max(remaining // (len(sections) - i), remaining - later_size)
        part = bounded_section(heading, lines, budget)
        if part:
            rendered.append(part)
            remaining -= len(part)
    text = header + "\n".join(rendered)
    return text, sorted(events, key=lambda e: (e["date"] or "", e["repo"].lower(), e["number"]))


def write_notebook(directory):
    text, events = notebook(ledger.load())
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "status.md").write_text(text, encoding="utf-8")
    (directory / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
    print(str(directory / "status.md"))
    print(str(directory / "events.jsonl"))


def mentioned(text, row):
    repo, number = canonical(row["repo"]).lower(), row["number"]
    if profile_readme.pull_mentioned(text, repo, number):
        return True
    for line in text.splitlines():
        preceding_repo = None
        for match in REFERENCE.finditer(line):
            if match["repo"]:
                preceding_repo = canonical(match["repo"]).lower()
            elif match["number"] == str(number) and preceding_repo == repo:
                return True
    return False


def destination_mentioned(text, row, destination, merged_counts):
    if destination == "profile":
        repo = canonical(row["repo"])
        counts = profile_readme.aggregate_counts(text, repo, CONFIG["github_user"])
        if counts:
            return row["status"] == "merged" and all(n == merged_counts[repo.lower()] for n in counts)
    return mentioned(text, row)


def audit_receipt(destination, url, digest):
    with path_from_config("ledger_file").with_suffix(".records.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        row = ledger.find(ledger.load(), url)
        if row is None or not unverified(records(row).get(destination)):
            return False
        updated = records(row)
        if digest is None:
            del updated[destination]
        else:
            updated[destination] = {"at": iso(), "ref": "audit", "sha256": digest}
        ledger.update_fields(url, records=updated)
        return True


def audit(interview_dir=None, readme=None, notebook_file=None, clear_missing=False):
    sources = {}
    for dest, value in (("profile", readme), ("teach", interview_dir), ("notebook", notebook_file)):
        if value is None:
            continue
        sources[dest] = destination_texts(value)
    changes, missing, cleared = [], [], []
    rows = ledger.load()
    merged_counts = profile_readme.merged_counts(rows)
    for row in rows:
        for dest, texts in sources.items():
            if not unverified(records(row).get(dest)):
                continue
            found = next((digest for _, text, digest in texts if destination_mentioned(text, row, dest, merged_counts)), None)
            entry = {"destination": dest, "url": row["url"]}
            if found:
                if audit_receipt(dest, row["url"], found):
                    changes.append({**entry, "ref": "audit"})
            else:
                missing.append(entry)
                if clear_missing and audit_receipt(dest, row["url"], None):
                    cleared.append(entry)
    return {"changed": changes, "missing": missing, "cleared": cleared,
            "skipped": [d for d in DESTINATIONS if d not in sources]}


def missing_references(destination, urls, file):
    rows = ledger.load()
    by_url = {row["url"]: row for row in rows}
    merged_counts = profile_readme.merged_counts(rows)
    for url in urls:
        row = by_url[url]
        repo = canonical(row["repo"])
        message = f'{url}: add {url} or "{repo} #{row["number"]}" on one line to {file}'
        if destination == "profile":
            aggregate = profile_readme.merged_url(repo, CONFIG["github_user"])
            message += f", or [{repo}]({aggregate}) ({merged_counts[repo.lower()]})"
            message += f"; bin/factory sync profile --readme {shlex.quote(str(file))} --write regenerates the block"
        yield message


def command(args):
    try:
        if args.sync_command == "status":
            print(json.dumps(status(), indent=2))
        elif args.sync_command == "profile":
            profile(args.readme, args.write)
        elif args.sync_command == "interview":
            interview(args.write)
        elif args.sync_command == "notebook":
            write_notebook(args.out)
        elif args.sync_command == "mark":
            result = mark(args.destination, args.url, file=args.file, ref=args.ref, deferred=args.deferred)
            print(json.dumps(records(result)[args.destination] if args.url is not None else result, indent=2))
            if args.url is None and result["missing"]:
                for message in missing_references(args.destination, result["missing"], args.file):
                    print(message, file=sys.stderr)
                return 1
        elif args.sync_command == "audit":
            print(json.dumps(audit(args.interview_dir, args.readme, args.notebook_file, args.clear_missing), indent=2))
        elif args.sync_command == "retro":
            result = retro.report(CONFIG["github_user"], args.since, write=args.write)
            print(json.dumps(result, indent=2))
    except (OSError, ValueError) as error:
        raise SystemExit(f"sync {args.sync_command} refused: {error}") from None
    return 0


def register(sub):
    parser = sub.add_parser("sync", help="derive local records and track destination receipts; never publishes")
    commands = parser.add_subparsers(dest="sync_command", required=True)
    commands.add_parser("status", help="list merged PRs still owed to each destination").set_defaults(fn=command)
    p = commands.add_parser("profile", help="diff the local profile's merged block; never pushes")
    p.add_argument("--readme")
    p.add_argument("--write", action="store_true")
    p.set_defaults(fn=command)
    p = commands.add_parser("interview", help="diff generated contribution tables and the index")
    p.add_argument("--write", action="store_true")
    p.set_defaults(fn=command)
    p = commands.add_parser("notebook", help="write a local status note and event outbox; never sends")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=command)
    p = commands.add_parser("mark", help="capture destination receipts from read-back text, or defer Private notebook")
    p.add_argument("destination", choices=DESTINATIONS)
    p.add_argument("url", nargs="?", help="one PR; omit to mark mentioned merged PRs still owed")
    p.add_argument("--ref", help="receipt label; defaults to the matching file name")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="read-back destination file, or directory of .md and .txt files")
    source.add_argument("--deferred", help="Private notebook retry reason; requires a PR URL")
    p.set_defaults(fn=command)
    p = commands.add_parser("audit", help="verify legacy receipts against explicitly supplied text")
    p.add_argument("--interview-dir")
    p.add_argument("--readme")
    p.add_argument("--notebook-file")
    p.add_argument("--clear-missing", action="store_true", help="remove absent unverified receipts only for supplied destinations")
    p.set_defaults(fn=command)
    p = commands.add_parser("retro", help="read GitHub statistics for submitted-since and merged-since populations")
    p.add_argument("--since", required=True)
    p.add_argument("--write", metavar="DIR")
    p.set_defaults(fn=command)
