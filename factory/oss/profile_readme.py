"""Render merged contributions and check profile coverage, counts and privacy."""
import base64
import binascii
import collections
import datetime as dt
import html
import json
import pathlib
import re
from urllib.parse import quote

from factory.core import ledger, metrics, metrics_site, privacy
from factory.core.repos import canonical
from factory.util import CONFIG, ROOT, GH_READ_TIMEOUT, CommandError, gh_json, path_from_config, public_destinations

LINK = re.compile(r"(?:https?://)?(?<![\w.-])(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)(/[^\s)\"'<>]*)?", re.I)
COUNT = re.compile(r"\)?[ \t]*\((\d+)\)")
PULL = re.compile(r"/pull/(\d+)(?!\d)")
PULL_LINK = re.compile(r"(?<![\w./:@-])(?:https?://)?(?:www\.)?github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)"
                       r"(?=$|[/?#\s)\]\"'<>]|[.,;](?=\s|$))", re.I)


START = "<!-- factory:merged:start -->"
END = "<!-- factory:merged:end -->"
PORTFOLIO_START = "<!-- factory:portfolio:start -->"
PORTFOLIO_END = "<!-- factory:portfolio:end -->"


def block(text, start=START, end=END):
    if start not in text and end not in text:
        return None
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) >= text.index(end):
        raise ValueError(f"keep exactly one ordered pair of {start} and {end}")
    return text.split(start, 1)[1].split(end, 1)[0]


def replace_block(text, content, start=START, end=END):
    if block(text, start, end) is None:
        raise ValueError(f"add {start} and {end} around the generated section")
    before, rest = text.split(start, 1)
    _, after = rest.split(end, 1)
    return before + start + "\n" + content.rstrip("\n") + "\n" + end + after


def load_groups():
    path = ROOT / "lines/oss/profile-groups.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read lines/oss/profile-groups.json; restore a valid groups file: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        raise ValueError("profile-groups.json must contain a groups list")
    for group in data["groups"]:
        if not isinstance(group, dict) or not isinstance(group.get("title"), str) or not isinstance(group.get("repos"), list):
            raise ValueError("each profile group needs a title and repos list")
        for entry in group["repos"]:
            if (not isinstance(entry, dict) or not isinstance(entry.get("repo"), str)
                    or not re.fullmatch(r"[\w.-]+/[\w.-]+", entry["repo"])
                    or not isinstance(entry.get("blurb"), str)):
                raise ValueError("each profile repo needs an owner/repo name and a blurb string")
    return data


def merged_url(repo, user):
    query = "is%3Apr+author%3A" + quote(user, safe="") + "+is%3Amerged"
    return f"https://github.com/{repo}/pulls?q={query}"


def aggregate_counts(text, repo, user):
    url = merged_url(canonical(repo), user)
    return [int(n) for n in re.findall(r"\[[^\]\r\n]*\]\(" + re.escape(url) + r"\) \((\d+)\)", text, re.I)]


def pull_mentioned(text, repo, number):
    repo = canonical(repo).lower()
    for match in PULL_LINK.finditer(text):
        if canonical(match[1]).lower() == repo and int(match[2]) == number:
            return True
    return False


def render(rows=None, groups=None, user=None):
    """Keep curated labels, joined entries and outside-block references in the groups file."""
    user = user or CONFIG["github_user"]
    rows = ledger_rows(user) if rows is None else rows
    rows = [r for r in rows if canonical(r["repo"]).split("/")[0].lower() != user.lower()]
    merged = merged_by_repo(rows)
    groups = load_groups() if groups is None else groups
    seen, sections = set(), []
    for group in groups["groups"]:
        entries = []
        previous_rendered, previous_blurb = False, None
        for entry in group["repos"]:
            repo = canonical(entry["repo"])
            joined = entry.get("join_previous")
            if joined and previous_blurb is None:
                raise ValueError(f"{repo} needs a preceding entry in profile-groups.json")
            blurb = entry["blurb"] or (previous_blurb if joined else "")
            previous_blurb = blurb
            key = repo.lower()
            if key in seen:
                raise ValueError(f"{repo} occurs twice in profile-groups.json; keep one entry")
            seen.add(key)
            numbers = merged.get(key, {}).get("numbers", set())
            if not numbers or entry.get("outside_block"):
                previous_rendered = False
                continue
            label = entry.get("label", repo.split("/")[1])
            if len(numbers) == 1:
                link = f"[{label}](https://github.com/{repo}/pull/{next(iter(numbers))})"
            else:
                link = f"[{label}]({merged_url(repo, user)}) ({len(numbers)})"
            if joined and previous_rendered:
                entries[-1][0] += " and " + link
                entries[-1][1] = blurb
            else:
                entries.append([link, blurb])
            previous_rendered = True
        if entries:
            heading = f"**{group['title']}**\n\n" if group["title"] else ""
            sections.append(heading + "\n".join(f"- {link}: {blurb}" for link, blurb in entries))
    missing = sorted(merged[key]["repo"] for key in merged.keys() - seen)
    if missing:
        raise ValueError("add merged repos to lines/oss/profile-groups.json: " + ", ".join(missing))
    return "\n\n".join(sections) + "\n"


def load_highlights():
    path = ROOT / "lines/oss/profile-highlights.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read lines/oss/profile-highlights.json; restore a valid highlights file: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("selected"), list):
        raise ValueError("profile-highlights.json must contain a selected list")
    active = data.get("active_contributor")
    if not isinstance(active, dict) or any(type(active.get(key)) is not int or active[key] < 1 for key in ("minimum_merged", "limit")):
        raise ValueError("profile-highlights.json needs positive minimum_merged and limit")
    labels = active.get("labels", {})
    if not isinstance(labels, dict) or any(not isinstance(repo, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo)
            or canonical(repo).lower() != repo or not isinstance(label, str) or not label.strip() for repo, label in labels.items()):
        raise ValueError("profile-highlights.json labels must map canonical lower-case repositories to display labels")
    for entry in data["selected"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("repo"), str)
                or not re.fullmatch(r"[\w.-]+/[\w.-]+", entry["repo"])
                or type(entry.get("number")) is not int or entry["number"] < 1 or not isinstance(entry.get("text"), str)):
            raise ValueError(f"invalid profile-highlights.json entry: {entry!r}")
    return data


def ordinal(number):
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def profile_date(value):
    try:
        date = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (date.astimezone(dt.timezone.utc) if date.tzinfo else date).date().isoformat()
    except (AttributeError, ValueError):
        return None


def load_influence():
    path = path_from_config("ledger_file").with_name("influence.jsonl")
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        data = json.loads(lines[-1]) if lines else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read latest influence reading: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("repos", []), list):
        raise ValueError("latest influence reading must contain a repos list")
    return data


def active_contributors(upstream, user, settings):
    merged = merged_by_repo(upstream)
    projects = [entry for entry in merged.values() if len(entry["numbers"]) >= settings["minimum_merged"]]
    if not projects:
        return ''
    labels = {canonical(entry["repo"]).lower(): entry.get("label", canonical(entry["repo"]).split('/')[1])
              for group in load_groups()["groups"] for entry in group["repos"]}
    labels.update(settings.get("labels", {}))
    projects.sort(key=lambda entry: (-len(entry["numbers"]), labels.get(entry["repo"].lower(), entry["repo"].split('/')[1]).lower()))
    reading = load_influence()
    ranks = {canonical(entry["repo"]).lower(): entry for entry in reading.get("repos", []) if isinstance(entry, dict) and isinstance(entry.get("repo"), str)}
    date = profile_date(reading.get("at"))
    entries, ranked = [], False
    for index, entry in enumerate(projects[:settings["limit"]]):
        repo = entry["repo"]
        label = labels.get(repo.lower(), repo.split('/')[1])
        detail = str(len(entry["numbers"])) + (" merged" if index == 0 else "")
        rank = ranks.get(repo.lower(), {})
        position, seen = rank.get("rank"), rank.get("contributors_seen")
        if date and type(position) is int and type(seen) is int and 0 < position <= seen:
            detail += f", {ordinal(position)} of {seen}" + (" contributors" if index == 0 else "")
            ranked = True
        entries.append(f"[{label}]({merged_url(repo, user)}) ({detail})")
    if not entries:
        return ''
    text = "**Active contributor to** " + ' · '.join(entries) + '.'
    return text + (f" Ranks are by commits in GitHub's contributor list on {date}." if ranked else '')


def portfolio_data(rows, user, generated_at=None):
    """Project at an explicit snapshot time, or at the retained observation time."""
    reader = metrics.Reader()
    ledger_path = path_from_config("ledger_file")
    snapshot = {"prs": rows, "pr_history": reader.json(ledger_path.with_name("pr-history.json")),
                "outcomes": reader.json(ledger_path.with_name("pr-outcomes.json"))}
    if generated_at is None:
        history = snapshot["pr_history"]
        check = history.get("reconciliation") if isinstance(history, dict) else None
        if isinstance(check, dict):
            activity = check.get("activity")
            generated_at = metrics.timestamp(activity.get("checked_at"), exact=True) if isinstance(activity, dict) else None
            generated_at = generated_at or metrics.timestamp(check.get("checked_at"), exact=True)
    return metrics.compute(snapshot, {**CONFIG, "github_user": user}, generated_at=generated_at)


def activity_alt(data):
    days = (data.get("activity") or {}).get("days") or []
    if not days:
        return "Daily activity: no dated events in this snapshot."
    totals = {key: sum(day[key] for day in days) for key in ("opened", "merged", "closed_unmerged")}
    return (f"Daily activity from {days[0]['date']} to {days[-1]['date']}, Asia/Singapore dates: "
            f"{totals['opened']} pull requests opened, {totals['merged']} merged and "
            f"{totals['closed_unmerged']} closed without merge. Each event is dated by when it happened.")


def published_snapshot(path):
    """The metrics.json a site publish wrote, so the card repeats that snapshot instead of recomputing it."""
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or not isinstance(data.get("generated_at"), str)
            or not all(isinstance(data.get(key), dict) for key in ("metrics", "cohort", "population"))):
        raise ValueError(f"{path} is not a published metrics.json")
    return data


def portfolio(rows, user, *, generated_at=None, data=None):
    upstream = [row for row in rows if canonical(row["repo"]).split("/")[0].lower() != user.lower()]
    merged = merged_by_repo(upstream)
    data = portfolio_data(upstream, user, generated_at=generated_at) if data is None else data
    repo, dashboard = public_destinations(CONFIG)
    settings = load_highlights()
    dates = sorted(date for row in upstream if (date := profile_date(row.get("submitted_at"))))
    since = f"Since {dates[0]} this" if dates else "This"
    disclosure = dt.date.fromisoformat(CONFIG["public"]["disclosure_since"]).isoformat()
    image_url = html.escape(dashboard, quote=True)
    parts = [f'<a href="{image_url}"><picture>'
             f'<source media="(max-width: 600px) and (prefers-color-scheme: dark)" srcset="{image_url}profile-dark-compact.svg">'
             f'<source media="(max-width: 600px)" srcset="{image_url}profile-light-compact.svg">'
             f'<source media="(prefers-color-scheme: dark)" srcset="{image_url}profile-dark.svg">'
             f'<img src="{image_url}profile-light.svg" width="880" alt="Run by Software Factory: merged pull requests, merge rate and merged-without-rework rate with the snapshot date, and the steps the factory takes. The counts are repeated in the text below."></picture></a>',
             f"**All in on AI.** {since} account's upstream contributions are run by [Software Factory]({dashboard}), an agent pipeline I design and operate. "
             f"It opens the pull requests, files the issues and writes the replies in other people's projects. From {disclosure} each new pull request says so.",
             "```text\n" + metrics_site.status_card(data) + "```",
             "Printed by `bin/factory metrics card` from the public snapshot named in its second line. The figures are redrawn each time the site is published; this block is rewritten when the profile is synced. Nothing on this page runs live.",
             f'<a href="{image_url}board.html"><picture><source media="(prefers-color-scheme: dark)" srcset="{image_url}activity-dark.svg">'
             f'<img src="{image_url}activity-light.svg" width="880" alt="{html.escape(activity_alt(data), quote=True)}"></picture></a>']
    active = active_contributors(upstream, user, settings["active_contributor"])
    if active:
        parts.append(active)
    selected = []
    for entry in settings["selected"]:
        name, number = canonical(entry["repo"]), entry["number"]
        if number not in merged.get(name.lower(), {}).get("numbers", set()):
            raise ValueError(f"profile-highlights.json entry {name}#{number} is not a merged upstream pull request")
        selected.append(f"- [{name.split('/')[1]}#{number}](https://github.com/{name}/pull/{number}): {entry['text']}")
    if selected:
        parts.append("**Selected pull requests**\n\n" + '\n'.join(selected))
    parts.append(f"[The factory]({dashboard}) · [Source](https://github.com/{repo}) · [Status board]({dashboard}board.html) · [Metric definitions](https://github.com/{repo}/blob/HEAD/docs/metrics.md)")
    return '\n\n'.join(parts) + '\n'


def render_readme(text, rows=None, groups=None, user=None, *, generated_at=None, data=None):
    user = user or CONFIG["github_user"]
    rows = ledger_rows(user) if rows is None else rows
    text = replace_block(with_markers(text), render(rows, groups, user))
    intro = portfolio(rows, user, generated_at=generated_at, data=data)
    if block(text, PORTFOLIO_START, PORTFOLIO_END) is not None:
        return replace_block(text, intro, PORTFOLIO_START, PORTFOLIO_END)
    return PORTFOLIO_START + "\n" + intro + PORTFOLIO_END + "\n\n" + text


def with_markers(text):
    """Wrap the existing upstream section, leaving Building and Earlier work untouched."""
    if block(text) is not None:
        return text
    heading = re.search(r"^\*\*Merged upstream[^\n]*\*\*\s*$", text, re.M)
    if not heading:
        return text.rstrip("\n") + "\n\n" + START + "\n" + END + "\n"
    start = heading.start()
    following = re.search(r"^(?:- Earlier:|\*\*(?!Merged upstream)|#{1,6} )", text[heading.end():], re.M)
    end = heading.end() + following.start() if following else len(text)
    return text[:start] + START + "\n" + text[start:end].rstrip("\n") + "\n" + END + "\n\n" + text[end:]


def fetch(user):
    try:
        data = gh_json("api", f"repos/{user}/{user}/contents/README.md", timeout=GH_READ_TIMEOUT)
    except (CommandError, ValueError) as error:
        raise ValueError(f"could not fetch {user}/{user} README.md: {error}") from None
    if not isinstance(data, dict) or data.get("encoding") != "base64" or not data.get("content"):
        raise ValueError(f"{user}/{user} README.md came back without base64 content")
    try:
        return base64.b64decode(data["content"]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as error:
        raise ValueError(f"{user}/{user} README.md is not valid base64 UTF-8: {error}") from None


def read(path):
    try:
        return pathlib.Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"cannot read --readme {path}: {error}") from None


def links(text):
    """(current owner/repo lowercased, path after the repo, offset where the link ends) for each github.com link."""
    for m in LINK.finditer(text):
        repo = m[2].removesuffix(".git")
        yield canonical(f"{m[1]}/{repo}").lower(), m[3] or "", m.end()


def ledger_rows(user):
    path = path_from_config("ledger_file")
    if not path.exists():
        raise ValueError(f"ledger {path} not found; nothing to compare the README with")
    rows = [r for r in ledger.load() if canonical(r["repo"]).split("/")[0].lower() != user.lower()]
    for r in rows:
        if r["status"] == "merged" and not isinstance(r.get("number"), int):
            raise ValueError(f"merged ledger row without a PR number: {r.get('url')}")
    return rows


def merged_by_repo(rows):
    merged = collections.defaultdict(lambda: {"repo": None, "numbers": set()})
    for r in rows:
        if r["status"] == "merged":
            name = canonical(r["repo"])
            entry = merged[name.lower()]
            entry["repo"] = name
            entry["numbers"].add(r["number"])
    return merged


def merged_counts(rows):
    return {repo: len(entry["numbers"]) for repo, entry in merged_by_repo(rows).items()}


def unrecorded(text, merged, groups, user):
    outside = {canonical(entry["repo"]).lower() for group in groups["groups"]
               for entry in group["repos"] if entry.get("outside_block")}
    found = []
    for key in sorted(outside & merged.keys()):
        entry = merged[key]
        repo, numbers = entry["repo"], entry["numbers"]
        counts = aggregate_counts(text, repo, user)
        if counts and all(n == len(numbers) for n in counts):
            continue
        if counts:
            found.append(f"the hand-maintained aggregate link for {repo} shows ({counts[0]}) but the ledger has "
                         f"{len(numbers)} merged; correct that count or remove it")
        for number in sorted(numbers):
            if pull_mentioned(text, repo, number):
                continue
            found.append(f"add https://github.com/{repo}/pull/{number} to the hand-maintained entry for {repo}")
    return found


def count_mismatches(text, rows, merged):
    known = {canonical(r["repo"]).lower(): canonical(r["repo"]) for r in rows}
    found = []
    for repo, _, end in links(text):
        count = COUNT.match(text, end)
        if not count or repo not in known:
            continue
        expected = len(merged[repo]["numbers"]) if repo in merged else 0
        row = {"repo": known[repo], "readme": int(count[1]), "ledger": expected}
        if row["readme"] != expected and row not in found:
            found.append(row)
    return found


def unlisted(tails, merged):
    found = []
    for repo, entry in sorted(merged.items()):
        pulls = [PULL.match(tail) for tail in tails.get(repo, [])]
        if not pulls or not all(pulls):
            continue
        linked = {int(m[1]) for m in pulls}
        missing = sorted(entry["numbers"] - linked)
        if missing:
            found.append({"repo": entry["repo"], "linked": sorted(linked), "unlisted": missing})
    return found


def privacy_findings(text):
    found = privacy.findings("README.md", text, prose=True)
    for address in CONFIG["git_email_previous"]:
        for m in re.finditer(re.escape(address), text, re.I):
            line = text.count("\n", 0, m.start()) + 1
            found.append(f"README.md:{line}: previous git email: {privacy.masked(address, 'previous git email')}")
    return list(dict.fromkeys(found))


def check(text, user, *, generated_at=None, data=None):
    missing = [key for key in ("private_terms", "git_email_previous") if key not in CONFIG]
    if missing:
        raise ValueError(f"{', '.join(missing)} not configured (.factory.local.json); the privacy check cannot run")
    rows = ledger_rows(user)
    merged = merged_by_repo(rows)
    groups = load_groups()
    tails = collections.defaultdict(list)
    for repo, tail, _ in links(text):
        tails[repo].append(tail)
    result = {
        "missing_repos": sorted((e["repo"] for key, e in merged.items() if key not in tails), key=str.lower),
        "count_mismatch": count_mismatches(text, rows, merged),
        "unlisted_prs": unlisted(tails, merged),
        "unrecorded_prs": unrecorded(text, merged, groups, user),
        "privacy_findings": privacy_findings(text),
    }
    intro = block(text, PORTFOLIO_START, PORTFOLIO_END)
    if intro is not None:
        result["portfolio_mismatch"] = [] if intro == "\n" + portfolio(rows, user, generated_at=generated_at, data=data) else [
            "run factory sync profile --readme PATH --write"]
    current = block(text)
    if current is not None:
        expected = "\n" + render(rows, groups, user)
        result["block_mismatch"] = [] if current == expected else ["run factory sync profile --readme PATH --write"]
    return {"ok": not any(result.values()), **result}


def run(readme=None):
    user = CONFIG["github_user"]
    return check(read(readme) if readme else fetch(user), user)
