"""Compose bounded role instructions from cards, item facts and routed lessons."""
import datetime as dt
import json
import re
import shlex

from factory.core import clones, ledger, lessons, review, timeline, verdicts
from factory.util import CONFIG, ROOT, CommandError, Item, clone_dir, now, path_from_config, read_json, run

BUDGET = 16000
TARGET = 15400
CARD_MAX_AGE_DAYS = 14
# Lowest priority first; craft notes within each group use name order.
SHORTEN_ORDER = ("policy", "tagged-craft", "default-craft", "upstream", "output")
ROLES = (
    "scout", "value-review", "implement", "review", "fix", "follow",
    "coordinator-review", "takeover", "record",
)
DEFAULT_TAGS = {
    "scout": ("selection",),
    "value-review": ("selection", "issue-first"),
    "implement": ("pr-text",),
    "review": ("pr-text",),
    "fix": ("pr-text",),
    "follow": ("pr-text",),
    "coordinator-review": ("pr-text",),
    "takeover": ("pr-text",),
    "record": (),
}
VARIANT = re.compile(r"<!-- (schema2|legacy) -->(.*?)<!-- /\1 -->", re.S)
SECTION = re.compile(r"^## Brief\s*\n(.*?)(?=^##? |\Z)", re.M | re.S)


def _read(path):
    if not path.is_file():
        raise ValueError(f"Missing brief input {path}; restore the role or craft card before retrying")
    return path.read_text().strip()


def _variant(text, schema):
    selected = "schema2" if schema >= 2 else "legacy"
    return VARIANT.sub(lambda match: match[2].strip() if match[1] == selected else "", text).strip()


def _card_notice(date):
    try:
        age = (now().date() - dt.date.fromisoformat(date)).days
    except (TypeError, ValueError):
        age = None
    if age is not None and 0 <= age <= CARD_MAX_AGE_DAYS:
        return ""
    dated = f"dated {date} ({age} days old)" if age is not None else "undated or invalidly dated"
    return (f"Verify first: {dated}; freshness threshold is {CARD_MAX_AGE_DAYS} days. "
            "Recheck upstream rules and packageManager/engines/runtime pins; "
            "do not install tool versions from this card.\n\n")


def _repo_card(repo):
    if not repo:
        return "No repository selected; use --repo owner/repo or an item."
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("Repository must be owner/repo; correct --repo or item.json")
    aliases = CONFIG.get("repo_renames", {})
    canonical = aliases.get(repo, repo)
    names = list(dict.fromkeys([repo, canonical] + [old for old, new in aliases.items() if new == canonical]))
    candidates = [ROOT / "lines/oss/repos" / (name.replace("/", "__") + ext)
                  for name in names for ext in (".md", ".json")]
    existing = []
    for path in candidates:
        if not path.is_file():
            continue
        existing.append(path)
        if path.suffix == ".json":
            data = read_json(path)
            value = data.get("brief")
            date = data.get("latest_evidence") or data.get("compiled") or data.get("date")
        else:
            text = path.read_text()
            match = SECTION.search(text)
            value = match[1] if match else None
            dated = re.search(r"Latest evidence:\s*(\d{4}-\d{2}-\d{2})", text) or re.search(r"Compiled:\s*(\d{4}-\d{2}-\d{2})", text)
            date = dated[1] if dated else None
        if isinstance(value, str) and value.strip():
            return f"## Repository conventions ({repo})\n\n{_card_notice(date)}{value.strip()}"
    if existing:
        return f"Required reading: {existing[0]} (this repository card has no Brief section)."
    return f"No repository card exists for {repo} in {ROOT / 'lines/oss/repos'}; read the upstream contribution rules."


def _facts(item, repo, pr, task):
    facts = {"repo": repo, "pr": pr}
    if item:
        facts.update({key: item.meta.get(key) for key in (
            "issue", "branch", "base", "kind", "implementer", "status", "test_cmd",
            "test_cwd", "test_files", "required_checks", "tags", "stacked_on", "review_budget_minutes",
            "target", "keywords",
        )})
        try:
            head = item.head_sha()
        except (CommandError, OSError) as exc:
            raise ValueError(f"Cannot read live head for {item.dir}; restore its clone/branch before requesting a brief: {exc}") from exc
        facts.update(item=str(item.dir), clone=str(item.clone), head=head, fix_rounds=review.rounds(item))
    if task:
        facts["task"] = task
    if not item:
        facts["item_resolution"] = "If the task names an existing item, rerun this role's brief with --item before working; these initial instructions assume a new schema-2 item."
    return facts


def _policy(item):
    text = ""
    flags = read_json(item.file("policy.json"), {}).get("flags", {}) if item else {}
    if flags:
        text += "Policy flag excerpts (first sentence, <=240 chars each). Read policy.json in full:\n"
        for flag, matches in flags.items():
            quote = matches[0] if isinstance(matches, list) and matches else matches
            if isinstance(quote, str):
                quote = re.split(r"(?<=[.!?])(?<!\d\.)\s+", quote.strip(), maxsplit=1)[0][:240]
            else:
                quote = "See policy.json"
            text += f"- {flag}: {quote}\n"
    return text


def _duties(item):
    return _read(ROOT / "lines/oss/craft/upstream.md") + "\n\n" + _policy(item)


def _fit(parts, optional):
    shortened = []
    texts = dict(parts)

    def render():
        text = "\n\n".join(texts[name] for name, _ in parts) + "\n"
        if shortened:
            text += "\nShortened sections: " + ", ".join(shortened) + ". Read the named files and sections before working.\n"
        return text

    result = render()
    for group in SHORTEN_ORDER:
        for name, (priority, reference) in sorted(optional.items()):
            if len(result) <= min(TARGET, BUDGET):
                return result
            if priority != group:
                continue
            replacement = f"Required reading: {reference}."
            if len(replacement) >= len(texts[name]):
                continue
            texts[name] = replacement
            shortened.append(name)
            result = render()
    if len(result) > BUDGET:
        sizes = ", ".join(f"{name}={len(text)}" for name, text in parts)
        raise ValueError(f"Brief exceeds {BUDGET} characters ({len(result)}) after required-reading fallbacks: {sizes}; "
                         "shorten task facts or split the task, never truncate mandatory rules or routed lessons")
    return result


def _substitute(text, item, repo, head=None):
    values = {"<repo>": shlex.quote(repo) if repo else "<owner/repo>"}
    if item:
        values.update({"<item>": shlex.quote(str(item.dir)), "<clone>": shlex.quote(str(item.clone))})
        if "<head>" in text:
            values["<head>"] = shlex.quote(head or item.head_sha())
        issue = item.meta.get("issue")
        target = f"{repo}#{issue}" if issue else repo
        keywords = item.meta.get("keywords") or []
        if not isinstance(keywords, list) or any(not isinstance(word, str) for word in keywords):
            raise ValueError("item.json keywords must be a list of phrases; correct them before requesting a brief")
        arguments = [target]
        for word in keywords:
            arguments.extend(["--keyword", word])
        if item.meta.get("target") == "discussion":
            arguments.append("--discussion")
        values["<target>"] = shlex.join(arguments)
        if issue:
            values["<issue>"] = str(issue)
    return re.sub("|".join(re.escape(key) for key in values), lambda match: values[match[0]], text)


def item_for_pr(pr):
    from factory.core.nextaction import local_item

    if not pr:
        return None
    url = pr.rstrip("/")
    row = next((r for r in ledger.load() if r.get("url", "").rstrip("/") == url), None)
    if row and row.get("item"):
        return local_item(row["item"])
    matches = []
    for path in sorted(path_from_config("runs_dir").rglob("item.json")):
        data = read_json(path)
        if isinstance(data, dict) and (data.get("pr_url") or "").rstrip("/") == url:
            matches.append(path.parent)
    if len(matches) > 1:
        raise ValueError(f"Multiple items match {pr}; choose the intended --item explicitly")
    return local_item(matches[0]) if matches else None


def compose(role, item=None, repo=None, pr=None, task=None):
    if role not in ROLES:
        raise ValueError(f"Unsupported role {role!r}; choose one of {', '.join(ROLES)}")
    if item is None and pr:
        item = item_for_pr(pr)
    if item is not None and not isinstance(item, Item):
        item = Item(item)
    if item and pr and item.meta.get("pr_url") and item.meta["pr_url"].rstrip("/") != pr.rstrip("/"):
        raise ValueError("--pr differs from item.json; use the item's pull request")
    if item and repo and repo != item.meta["repo"]:
        raise ValueError("--repo differs from item.json; use the item's repository")
    repo = item.meta["repo"] if item else repo
    pr = pr or (item.meta.get("pr_url") if item else None)
    schema = verdicts.schema(item) if item else 2
    directory = ROOT / "lines/oss"
    facts = _facts(item, repo, pr, task)
    if role == "scout" and repo and not item:
        facts["clone"] = str(clone_dir(repo))
    card = _substitute(_variant(_read(directory / "roles" / f"{role}.md"), schema), item, repo, facts.get("head"))
    parts = [
        ("common", _read(directory / "roles/_common.md")),
        (f"role:{role}", card),
        ("facts", "## Item facts\n\n" + json.dumps(facts, ensure_ascii=False)),
        ("repo", _repo_card(repo)),
    ]
    tags = (item.meta.get("tags") or []) if item else []
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError("item.json tags must be a list of craft-note names")
    optional = {}
    for tag in dict.fromkeys([*tags, *DEFAULT_TAGS[role]]):
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", tag):
            raise ValueError(f"Invalid craft tag {tag!r}; use a name from lines/oss/craft")
        path = directory / "craft" / f"{tag}.md"
        if path.is_file():
            parts.append((f"craft:{tag}", _read(path)))
            priority = "default-craft" if tag in DEFAULT_TAGS[role] else "tagged-craft"
            optional[f"craft:{tag}"] = (priority, f"{path.relative_to(ROOT)} (whole note)")
        elif tag in DEFAULT_TAGS[role]:
            _read(path)
        else:
            parts.append((f"craft:{tag}", f"No craft note for item tag {tag}; retain the tag in the handoff."))
    selected = sorted(lessons.select(role, repo), key=lambda line: not line.get("must", False))
    routed = "\n".join(f"- {line['id']}{' [must]' if line.get('must') else ''}: {line['brief']}" for line in selected)
    parts.append(("lessons", "## Routed lessons\n\n" + (routed or "No routed lessons for this role and repository.")))
    parts.append(("upstream", _read(directory / "craft/upstream.md")))
    optional["upstream"] = ("upstream", "lines/oss/craft/upstream.md (Upstream duties)")
    policy = _policy(item)
    if policy:
        parts.append(("policy", policy))
        optional["policy"] = ("policy", f"{item.file('policy.json')} (flags)")
    check = re.search(r"^## Check\s*\n(.*?)(?=^## |\Z)", card, re.M | re.S)
    if not check or not check[1].strip():
        raise ValueError(f"Role {role} has no Check command; repair its card")
    parts.append(("output", _read(directory / "craft/handoff.md")))
    optional["output"] = ("output", "lines/oss/craft/handoff.md (Output contract)")
    return _fit(parts, optional)


class _BudgetItem(Item):
    sizing_head_reason = None

    def head_sha(self):
        self.sizing_head_reason = None
        if not self.clone.exists():
            self.sizing_head_reason = "absent clone"
            return "0" * 40
        try:
            return super().head_sha()
        except CommandError:
            probe = run(["git", "-C", str(self.clone), "rev-parse", "--verify", "--quiet", "--end-of-options",
                         self.meta["branch"] + "^{commit}"], check=False)
            if probe.returncode != 1:
                raise
            self.sizing_head_reason = "missing ref"
            return "0" * 40


def check_all():
    """Measure retained active-lane items and every itemless lane without timeline writes."""
    lanes = read_json(ROOT / "lines/oss/lanes.json")
    rows = [row for group in lanes.values() for row in group]
    repos = sorted({row["repo"] for row in rows})
    active = {row["repo"].casefold() for row in rows if row["state"] == "active"}
    items = []
    errors = []
    for path in sorted(path_from_config("runs_dir").rglob("item.json")):
        try:
            item = _BudgetItem(path.parent)
            if item.meta["repo"].casefold() in active:
                items.append(item)
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
            errors.append(f"ERROR {path}: {exc}; repair item.json before checking all briefs")
    absent = sum(not item.clone.exists() for item in items)
    print(f"Budget sweep: {len(items)} retained items, {len(repos)} itemless repositories; "
          f"{absent} absent clones use a 40-character sizing head (not execution evidence).")
    print("role\tmaximum\ttarget")
    for role in ROLES:
        maximum = (0, "-")
        for item, repo in [(item, None) for item in items] + [(None, repo) for repo in repos]:
            target = str(item.dir) if item else repo
            try:
                size = len(compose(role, item, repo=repo))
                maximum = max(maximum, (size, target))
                if size > BUDGET:
                    errors.append(f"OVERFLOW {role} {target}: {size} > {BUDGET}; shorten mandatory inputs")
            except (ValueError, OSError) as exc:
                kind = "OVERFLOW" if str(exc).startswith("Brief exceeds") else "ERROR"
                size = re.search(r"Brief exceeds \d+ characters \((\d+)\)", str(exc))
                if size:
                    maximum = max(maximum, (int(size[1]), target))
                errors.append(f"{kind} {role} {target}: {exc}")
        print(f"{role}\t{maximum[0]}\t{maximum[1]}")
    for item in items:
        if item.sizing_head_reason == "missing ref":
            print(f"SIZING {item.dir}: missing ref {item.meta['branch']}; 40-character placeholder, not execution evidence")
    for error in errors:
        print(error)
    return 1 if errors else 0


def cmd_brief(args):
    if getattr(args, "check_all", False):
        if any((args.role, args.item, args.repo, args.pr, args.task)):
            raise SystemExit("Use brief --check-all without role, item, repo, pr or task filters")
        return check_all()
    if not args.role:
        raise SystemExit("Choose a brief role or use brief --check-all")
    try:
        item = Item(args.item) if args.item else item_for_pr(args.pr)
        if item and args.pr and item.meta.get("pr_url") and item.meta["pr_url"].rstrip("/") != args.pr.rstrip("/"):
            raise ValueError("--pr differs from item.json; use the item's pull request")
        if item and args.repo and args.repo != item.meta["repo"]:
            raise ValueError("--repo differs from item.json; use the item's repository")
        if item and args.role in {"implement", "review", "fix", "follow", "coordinator-review", "takeover"}:
            clones.ensure(item)
        elif args.role == "scout" and args.repo and not item:
            clones.ensure_repo(args.repo)
        text = compose(args.role, item, args.repo, args.pr, args.task)
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
    if item:
        timeline.mark(item, f"{args.role}-started", head=item.head_sha())
    print(text, end="")


def register(sub):
    parser = sub.add_parser("brief", help="Print a self-contained role brief")
    parser.add_argument("role", choices=ROLES, nargs="?")
    parser.add_argument("--check-all", action="store_true", help="Read-only size sweep of retained active-lane items and all lane repositories")
    parser.add_argument("--item")
    parser.add_argument("--repo")
    parser.add_argument("--pr")
    parser.add_argument("--task")
    parser.set_defaults(fn=cmd_brief)
    from factory.core import provenance
    provenance.register(sub)
