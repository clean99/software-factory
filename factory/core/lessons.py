"""Parse, validate and route lessons to the roles that can act on them."""
import fcntl
import json
import re

from factory.util import iso, path_from_config

STAGES = ("select", "qualify", "build", "verify", "review", "ship", "followup", "record", "environment", "meta")
ROLES = ("scout", "value-review", "implement", "review", "fix", "follow", "coordinator-review", "takeover", "record")
STATUSES = ("candidate", "active", "disputed", "retired")
KINDS = ("check", "brief", "card", "procedure", "retired")
HEADER = re.compile(r"^## (L-\d{3,}) · (.+)$")
FIELD = re.compile(r"^- ([\w -]+):(?: (.*))?$")
REPO = re.compile(r"[\w.-]+/[\w.-]+", re.ASCII)
TOKEN = re.compile(r"(check|brief):([a-z][a-z0-9-]*)|card:([\w.-]+/[\w.-]+)|(?:procedure|retired):([^,\r\n]+)", re.ASCII)


def parse(text):
    entries, entry = [], None
    for line in text.splitlines():
        header = HEADER.fullmatch(line)
        if header:
            entry = {"id": header[1], "title": header[2]}
            entries.append(entry)
        elif line.startswith("## "):
            entry = None
        elif entry is not None:
            field = FIELD.fullmatch(line)
            if field:
                key, value = field[1], field[2] or ""
                entry[key] = value == "true" if key == "must" and value in ("true", "false") else value
    return entries


def format_block(entry):
    fields = []
    for key, value in entry.items():
        if key in ("id", "title"):
            continue
        if isinstance(value, bool):
            value = str(value).lower()
        fields.append(f"- {key}: {value}")
    return f"## {entry['id']} · {entry['title']}\n\n" + "\n".join(fields) + "\n"


def load():
    path = path_from_config("lessons_file")
    return parse(path.read_text()) if path.exists() else []


def enforcement(entry):
    return [part.strip() for part in entry.get("enforced-by", "").partition(" — ")[0].split(",")]


def routing_scope(entry):
    return entry.get("routing-scope", entry.get("scope", ""))


def lint(entries=None):
    from factory.core import checks, review

    known_checks = set(checks.LEGACY) | set(checks.registry())
    problems, seen = [], set()
    for entry in load() if entries is None else entries:
        lid = entry["id"]
        if lid in seen:
            problems.append(f"{lid}: duplicate id; assign a new lesson id")
        seen.add(lid)
        if entry.get("stage") not in STAGES:
            problems.append(f"{lid}: unknown stage {entry.get('stage')!r}; choose {', '.join(STAGES)}")
        if entry.get("status") not in STATUSES:
            problems.append(f"{lid}: unknown status; choose {', '.join(STATUSES)}")
        if entry.get("status") == "active" and entry.get("superseded-by"):
            problems.append(f"{lid}: active lesson has superseded-by; retire it or remove superseded-by")
        if "must" in entry and not isinstance(entry["must"], bool):
            problems.append(f"{lid}: must must be true or false")
        if entry.get("status") == "candidate" and entry.get("must") is True:
            problems.append(f"{lid}: candidate cannot be must; remove must or establish the rule as active")
        if "routing-scope" in entry and not (routing_scope(entry).lower() == "global" or REPO.fullmatch(routing_scope(entry))):
            problems.append(f"{lid}: routing-scope must be global or owner/repo")
        brief = entry.get("brief", "")
        if len(brief) > 240:
            problems.append(f"{lid}: brief is {len(brief)} characters; shorten it to at most 240")
        for token in enforcement(entry):
            if not TOKEN.fullmatch(token):
                problems.append(f"{lid}: invalid enforced-by token {token!r}; use check:, brief:, card:, procedure: or retired:")
                continue
            kind, value = token.split(":", 1)
            if token == "procedure:route-this-lesson":
                problems.append(f"{lid} is not routed: set stage, enforced-by and brief")
            if kind == "check" and value not in known_checks:
                problems.append(f"{lid}: unknown check {value!r}; use an id in checks.LEGACY or registry()")
            if kind == "brief":
                if value not in ROLES:
                    problems.append(f"{lid}: unknown brief role {value!r}; choose {', '.join(ROLES)}")
                if not brief.strip():
                    problems.append(f"{lid}: brief:{value} requires a brief line; add one imperative sentence")
    return problems


def select(role, repo=None):
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; choose {', '.join(ROLES)}")
    scopes = {"global", (repo or "").casefold()} - {""}
    selected = [entry for entry in load()
                if entry.get("status") in ("active", "candidate")
                and f"brief:{role}" in enforcement(entry)
                and routing_scope(entry).casefold() in scopes]
    selected.sort(key=lambda entry: (entry.get("must") is not True,
                                    routing_scope(entry).casefold() == "global", -int(entry["id"][2:])))
    return [{"id": entry["id"], "brief": entry.get("brief", ""), "scope": routing_scope(entry),
             "must": entry.get("must") is True} for entry in selected]


def stats():
    entries = load()
    result = {"total": len(entries), "status": {}, "stage": {}, "enforcement": dict.fromkeys(KINDS, 0)}
    for entry in entries:
        for key in ("status", "stage"):
            value = entry.get(key, "missing")
            result[key][value] = result[key].get(value, 0) + 1
        for kind in {token.split(":", 1)[0] for token in enforcement(entry) if TOKEN.fullmatch(token)}:
            result["enforcement"][kind] += 1
    return result


def _append(values, pr=None, allow_unrouted=False):
    from factory.core import ledger

    for key, value in values.items():
        if isinstance(value, str) and ("\n" in value or "\r" in value):
            raise SystemExit(f"{key} must fit on one line; remove line breaks before adding the lesson")
    for key in ("title", "rule", "scope"):
        if not values[key].strip():
            raise SystemExit(f"{key} cannot be empty; provide the lesson's {key}")
    path = path_from_config("lessons_file")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        text = handle.read()
        entries = parse(text)
        number = max((int(entry["id"][2:]) for entry in entries), default=0) + 1
        entry = {"id": f"L-{number:03d}", **values}
        problems = lint([entry])
        if allow_unrouted:
            debt = f"{entry['id']} is not routed: set stage, enforced-by and brief"
            problems = [problem for problem in problems if problem != debt]
        if problems:
            raise SystemExit("\n".join(problems))
        handle.seek(0, 2)
        if not text:
            prefix = ""
        elif text.endswith("\n"):
            prefix = "\n"
        else:
            prefix = "\n\n"
        handle.write(prefix + format_block(entry))
    if pr:
        with ledger.locked():
            rows = ledger.load()
            row = ledger.find(rows, pr)
            if row:
                row["lesson"] = entry["id"]
                if row.get("status") == "closed" and row.get("closed_at"):
                    row["lesson_closed_at"] = row["closed_at"]
                ledger.save(rows)
    return entry["id"]


def add(args):
    if args.scope.casefold() != "global" and not REPO.fullmatch(args.scope):
        raise SystemExit("scope must be global or owner/repo; put the condition in the rule and brief")
    values = {"title": args.title, "status": args.status, "scope": args.scope,
              "source": f"{args.source or args.pr or ''} ({iso()[:10]})", "rule": args.rule,
              "stage": args.stage, "enforced-by": args.enforced_by}
    if args.brief is not None:
        values["brief"] = args.brief
    if args.must:
        values["must"] = True
    return _append(values, args.pr)


def add_legacy(title, rule, scope, source, enforced_by, status="active", pr=None):
    values = {"title": title, "status": status, "scope": scope,
              "source": f"{source or pr or ''} ({iso()[:10]})", "rule": rule,
              "stage": "meta", "enforced-by": enforced_by}
    tokens = enforcement(values)
    if not all(TOKEN.fullmatch(token) for token in tokens):
        values["enforced-by"] = "procedure:route-this-lesson — " + enforced_by
    else:
        roles = [token.split(":", 1)[1] for token in tokens if token.startswith("brief:")]
        if roles:
            stages = {"scout": "select", "value-review": "qualify", "implement": "build",
                      "fix": "build", "follow": "followup", "record": "record", "takeover": "review"}
            values["stage"] = stages.get(roles[0], "review")
            values["brief"] = re.split(r"(?<=[.!?])\s", rule.strip(), maxsplit=1)[0]
    return _append(values, pr, allow_unrouted=True)


def command(args):
    if args.action == "lint":
        problems = lint()
        print("\n".join(problems) if problems else "PASS: lessons lint")
        if problems:
            raise SystemExit(1)
    elif args.action == "stats":
        print(json.dumps(stats(), indent=2, ensure_ascii=False))
    elif args.action == "add":
        print(add(args))
    else:
        entries = select(args.role, args.repo) if args.role else load()
        if args.repo and not args.role:
            entries = [entry for entry in entries if routing_scope(entry).casefold() in ("global", args.repo.casefold())]
        print(json.dumps(entries, indent=2, ensure_ascii=False))


def register(sub):
    parser = sub.add_parser("lessons", help="lint, route and record lessons")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("lint", "stats"):
        actions.add_parser(name).set_defaults(fn=command)
    listing = actions.add_parser("list")
    listing.add_argument("--role", choices=ROLES)
    listing.add_argument("--repo")
    listing.set_defaults(fn=command)
    adding = actions.add_parser("add")
    adding.add_argument("--title", required=True)
    adding.add_argument("--rule", required=True)
    adding.add_argument("--scope", default="global")
    adding.add_argument("--source")
    adding.add_argument("--status", choices=STATUSES, default="active")
    adding.add_argument("--pr")
    adding.add_argument("--stage", choices=STAGES, required=True)
    adding.add_argument("--enforced-by", required=True)
    adding.add_argument("--brief")
    adding.add_argument("--must", action="store_true")
    adding.set_defaults(fn=command)
