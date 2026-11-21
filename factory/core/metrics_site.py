"""Static portfolio rendering; metric computation stays in metrics.py."""
import base64
import datetime as dt
import hashlib
import html
import json
import math
import pathlib
import re
import textwrap
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from factory.core.metrics import GROUPS, PR_URL, github_link, public_links, timestamp
from factory.util import CONFIG, CommandError

REASONS = {
    "author-update": ("The factory changed it after opening", "Usually after a review comment, often from a bot. Owned by the required checks and independent review."),
    "change-requested": ("A maintainer formally requested changes", "Owned by independent review."),
    "maintainer-repair": ("A maintainer repaired it before merging", "Owned by build and independent review."),
    "no-real-user": ("No real-world case shown", "Owned by value review."),
    "works-as-designed": ("Works as designed", "Owned by value review."),
    "approach-declined": ("Approach declined", "Owned by value review."),
    "duplicate": ("Someone else fixed it first", "Owned by qualify."),
    "superseded": ("The maintainer fixed it another way", "Owned by build: fix the layer that owns the contract."),
    "process": ("A project rule was not followed", "Owned by the repository card."),
    "withdrawn": ("Withdrawn by the factory", "Owned by independent review."),
    "unexplained": ("No reason given", "No owner."),
    "unclassified": ("Not classified yet", "Counts against the factory until someone records a reason."),
    "no-reply": ("No reply from a person", "Owned by project selection. The factory has stopped opening pull requests there."),
    "in-review": ("In review", "Waiting is the maintainers' time. It is not measured."),
    "agreement": ("Waiting on a contributor agreement", "Owner action."),
    "unchecked": ("Not checked yet", "Follow-up has not read this thread yet."),
}
OUTCOMES = {"merged_no_rework": "Merged without rework", "merged_reworked": "Merged after rework",
            "merged_unverified": "Merged, history unavailable", "closed": "Closed without merge", "open": "Still open"}
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
THEMES = {
    "dark": {"panel": "#0d1117", "edge": "#30363d", "grid": "#161b22", "ink": "#e6edf3", "muted": "#8b949e",
             "belt": "#484f58", "house": "#161b22", "flow": "#ff8a3d", "good": "#58a6ff", "off": "#6e7681", "fill": "#3987e5", "fill2": "#184f95", "track": "#21262d", "row": "#161b22"},
    "light": {"panel": "#ffffff", "edge": "#d0d7de", "grid": "#f3f5f7", "ink": "#1f2328", "muted": "#59636e",
              "belt": "#afb8c1", "house": "#f6f8fa", "flow": "#d1510b", "good": "#0969da", "off": "#8c959f", "fill": "#2a78d6", "fill2": "#86b6ef", "track": "#e6ebf0", "row": "#f6f8fa"},
}
STATIONS = [
    ("INTAKE", "scout agent"), ("SELECT", "CLI + value review"), ("BUILD", "implement agent"),
    ("VERIFY", "second agent"), ("GATE", "coordinator + CLI"), ("DELIVER", "CLI"), ("OUTCOME", "maintainers"),
]
WIDTH, HEIGHT = 960, 452
BELT_Y, PITCH, FIRST_X, SPEED = 196, 116, 84, 120
BIN_Y, LOOP_Y, SORT_X, BOARD_X = 318, 262, 846, 866
ROWS = {"merged": (132, "good"), "open": (BELT_Y, "flow"), "closed": (260, "off")}

STEPS = [
    ("scout", "reads a problem someone reported"),
    ("select", "asks who hits it and whether the project wants a fix"),
    ("build", "writes the fix and a test that fails without it"),
    ("verify", "a second agent tries to break it"),
    ("gate", "reruns the proof, scans, binds the exact commit"),
    ("ship", "opens the pull request and says it is automated"),
]
STEP_SECONDS = 2

def escape(value):
    return html.escape(str(value), quote=True)


def value_text(value, unit):
    if value is None:
        return "Not tracked"
    if not isinstance(value, (int, float)):
        return str(value)
    if unit == "percent":
        return f"{value * 100:.1f}%"
    if unit == "hours":
        return f"{value / 24:.1f} d" if value >= 48 else f"{value:.1f} h"
    if unit in ("minutes", "seconds", "rounds", "calls"):
        return f"{value:,.1f} {unit}" if isinstance(value, float) else f"{value:,} {unit}"
    return f"{value:,}"


def table(headers, rows, name="Recorded measurements"):
    return f'<div class="table-scroll" tabindex="0" role="region" aria-label="{escape(name)}"><table><thead><tr>' + ''.join(f'<th scope="col">{escape(h)}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join(
        '<tr>' + ''.join(f'<td>{cell}</td>' for cell in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def cells(values, *, numeric_from=None, date=False):
    return '<tr>' + ''.join('<td' + (' class="num"' if numeric_from is not None and i >= numeric_from else
                                   ' class="date"' if date and i == 0 else '') + f'>{value}</td>'
                           for i, value in enumerate(values)) + '</tr>'


def link(url, label):
    return f'<a href="{escape(url)}">{escape(label)}</a>' if github_link(url, api=False) else escape(label)


def pr_link(url):
    if not isinstance(url, str) or not PR_URL.fullmatch(url):
        return ''
    parts = url.rstrip('/').split('/')
    return link(url, f'{parts[-3]}#{parts[-1]}')


def opened_dates(data):
    return sorted(row["opened"] for row in data["pull_requests"] if row["opened"])


def figure_data(data):
    dates = opened_dates(data)
    metrics = data["metrics"]
    return {"as_of": data["cohort"]["as_of"], "since": dates[0] if dates else '—',
            "candidates": metrics["candidates_found"]["value"], "stopped": metrics["candidates_stopped_before_publish"]["value"],
            **{key: metrics['prs_' + key]["value"] for key in ("opened", "merged", "closed", "open")},
            "merged_total": metrics["prs_merged"]["value"], "merged_projects": data["population"]["merged_projects"],
            "cohort": data["cohort"],
            "rates": [metrics[key] for key in ("cohort_merge_rate", "merged_no_rework_share")]}


def count_text(value):
    return "n/a" if value is None else str(value)


def line_label(data):
    counts = {key: count_text(value) for key, value in data.items()}
    label = (f"The factory line on {counts['as_of']}: of {counts['candidates']} recorded candidates, {counts['stopped']} were "
             f"stopped before a pull request and {counts['opened']} were opened. {counts['merged']} were merged, "
             f"{counts['closed']} were closed without merging and {counts['open']} are still open. Stations: intake, select, build, verify, gate, "
             "deliver, outcome. Select, verify and gate can take a change off the line.")
    missing = [key for key in ("candidates", "stopped", "opened", "merged", "closed", "open") if data[key] is None]
    return label + (" Counts not measured: " + ', '.join(missing) + "." if missing else "")


def rate_text(row):
    return f'{count_text(row["numerator"])} of {count_text(row["denominator"])}' if row["value"] is None else f'{row["value"] * 100:.0f}%'


def rate_detail(row, merge=False):
    if row["value"] is None:
        return "Too few to show a rate"
    counts = f'{row["numerator"]} of {row["denominator"]}'
    if not merge:
        return counts + " merges: no change request, no fix from the factory, no repair by a maintainer"
    lower, upper = row["interval"]
    return counts + f" merged · 95% interval {lower * 100:.0f} to {upper * 100:.0f}%"


def date_ticks(count):
    return set(range(count)) if count <= 8 else {0, (count - 1) // 2, count - 1}


def chart(rows, title, series, unit):
    """Event counts share a zero-based scale; the template supplies the table twins."""
    values = [row[key] for row in rows for key, _ in series if row[key] is not None]
    if not rows or not values:
        return '<p class="empty">No dated observations yet.</p>'
    peak = max(1, max(values))
    tick_step = 10 ** math.floor(math.log10(peak)) / 2
    maximum = math.ceil(peak / tick_step) * tick_step
    left, top, width, height = 44, 20, 456, 176
    step = width / len(rows)

    def x(index):
        return left + (index + .5) * step

    def y(value):
        return top + height * (1 - value / maximum)

    parts = [f'<svg viewBox="0 0 520 250" role="img" aria-label="{escape(title)}; exact values in the table below">',
             f'<title>{escape(title)}</title>']
    for tick in (0, maximum / 2, maximum):
        parts.append(f'<path class="grid-line" d="M{left} {y(tick):.2f}H{left + width}"/>')
        parts.append(f'<text class="axis" x="{left - 8}" y="{y(tick) + 4:.2f}" text-anchor="end">{tick:g}</text>')
    for i, row in enumerate(rows):
        at = timestamp(row["week"])
        date = f'{at.day} {at:%b}' if at else row["week"]
        tip = f'Week of {date}: ' + ', '.join(f'{label.lower()} {value_text(row[key], unit)}' for key, label in series)
        parts.append(f'<g class="mark-group" tabindex="0" data-tip="{escape(tip)}"><title>{escape(tip)}</title>')
        bar_width = min(16, max(.1, (step - 6) / len(series)))
        for s, (key, _) in enumerate(series):
            value = row[key]
            if not value:
                continue
            bx = x(i) + (s - (len(series) - 1) / 2) * (bar_width + 2) - bar_width / 2
            by = y(value)
            radius = min(4, bar_width / 2, (top + height - by) / 2)
            d = (f'M{bx:.2f} {top + height}V{by + radius:.2f}Q{bx:.2f} {by:.2f} {bx + radius:.2f} {by:.2f}'
                 f'H{bx + bar_width - radius:.2f}Q{bx + bar_width:.2f} {by:.2f} {bx + bar_width:.2f} {by + radius:.2f}V{top + height}Z')
            parts.append(f'<path class="series-{s + 1} bar-mark" d="{d}"/>')
        parts.append(f'<rect class="hit-area" x="{left + i * step:.2f}" y="{top}" width="{step:.2f}" height="{height}"/></g>')
        if i in date_ticks(len(rows)):
            parts.append(f'<text class="axis date-axis" x="{x(i):.2f}" y="222" text-anchor="middle">{escape(date)}</text>')
    parts.append('</svg>')
    legend = '<div class="chart-legend">' + ''.join(f'<span><i class="swatch series-{s + 1}" aria-hidden="true"></i>{escape(label)}</span>' for s, (_, label) in enumerate(series)) + '</div>'
    return legend + ''.join(parts) + '<div class="tooltip" hidden></div>'


def reason_rows(rows):
    maximum = max((row["n"] for row in rows), default=0)
    parts = []
    for row in sorted(rows, key=lambda r: r["key"] == "unclassified"):
        label, owner = REASONS.get(row["key"], REASONS["unclassified"])
        links = ' '.join(pr_link(url) for url in row["pull_requests"]) if row["n"] <= 4 else ''
        width = 100 * row["n"] / maximum if maximum else 0
        parts.append(f'<li><span>{escape(label)}</span><span class="n">{escape(row["n"])}</span>'
                     f'<span class="bar"><i style="width:{width:.0f}%"></i></span>'
                     f'<span class="lever">{escape(owner)}' + (' ' + links if links else '') + '</span></li>')
    return ''.join(parts)


def measurement_tables(metrics):
    sections = []
    for group in GROUPS:
        rows = []
        for row in metrics.values():
            if row["group"] != group:
                continue
            coverage = row["coverage"]
            detail = escape(row["definition"])
            if row.get("total_pushes") is not None:
                detail += f'<p class="detail-note">{escape(row["total_pushes"])} commit/force-push updates in total.</p>'
            if "samples" in row:
                detail += f'<p class="detail-note">{escape(row["samples"])}/{escape(row["requests"])} change requests have a subsequent fix.</p>'
            for field in ("reason", "note"):
                if row.get(field):
                    detail += '<p class="detail-note">' + escape(row[field]) + '</p>'
            rows.append([f'<span class="metric-name">{escape(row["label"])}</span>',
                         f'<strong>{escape(value_text(row["value"], row["unit"]))}</strong><span class="status">{escape(row["status"].replace("_", " "))}</span>',
                         detail, f'<span class="coverage">{escape(coverage["covered"])}/{escape(coverage["total"])}</span><small>{escape(row["source"])}</small>'])
        sections.append(f'<section class="metrics-section" id="{group.lower().replace(" ", "-")}"><h2>{escape(group)}</h2>'
                        + table(["Metric", "Recorded value", "Definition & limits", "Coverage & source"], rows) + '</section>')
    return ''.join(sections)


def reconciliation_text(row):
    if row.get("github") is None or row.get("ledger") is None:
        return "The comparison has not run for this snapshot."
    if row["github"] == row["ledger"]:
        return f'On this snapshot both list {row["github"]}.'
    return f'On this snapshot GitHub lists {row["github"]} and the ledger lists {row["ledger"]}.'


def repository_rows(rows, user):
    parts = []
    for row in rows:
        repository = row["repository"]
        if repository == "other":
            name = escape(f'{row["repositories"]} other projects, fewer than five each')
        elif isinstance(repository, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", repository, re.ASCII):
            name = link(f'https://github.com/{repository}/pulls?' + urlencode({"q": f'is:pr author:{user}'}), repository)
        else:
            name = escape(repository)
        parts.append(cells([name] + [escape(row[key]) for key in ("opened", "merged", "merged_no_rework", "closed", "open")], numeric_from=1))
    return ''.join(parts)


def pull_request_rows(rows):
    parts = []
    for row in rows:
        reason = REASONS.get(row["reason"], REASONS["unclassified"])[0] if row["reason"] else '—'
        reason = escape(reason) + (' ' + link(row["evidence"], "evidence") if row.get("evidence") else '')
        parts.append(cells([link(row["url"], f'{row["repository"]}#{row["number"]}'), escape(row["opened"] or '—'),
                            escape(OUTCOMES.get(row["bucket"], row["bucket"] or '—')), reason]))
    return ''.join(parts)


def improvement_rows(rows, repository):
    parts = []
    for row in rows:
        evidence = ' '.join(link(item.get("url") or repository + '/blob/HEAD/' + item["path"], item["label"]) for item in row["links"])
        parts.append(cells([escape(row["date"]), escape(row["happened"]) + (' ' + evidence if evidence else ''),
                            escape(row["change"]), escape(row["moves"])], date=True))
    return ''.join(parts)


def weekly_rows(rows, keys):
    return ''.join(cells([escape(row["week"])] + [escape(row[key]) for key in keys], numeric_from=1) for row in rows)


def fact_values(metrics):
    facts = (
        ("CANDIDATES", ("candidates_found", "prs_opened", "candidates_stopped_before_publish")),
        ("REVIEW", ("sent_back_review",)), ("SHIFTS", ("shift_completion_rate",)),
        ("TESTS", ("factory_test_count", "factory_test_runtime")),
    )
    result = {}
    for name, keys in facts:
        rows = [metrics[key] for key in keys]
        missing = [row for row in rows if row["value"] is None]
        if missing:
            value, detail = "Not measured", ' '.join(row["reason"] or '' for row in missing)
        else:
            value = value_text(rows[0]["value"], "count")
            if name == "CANDIDATES":
                detail = f'{rows[1]["value"]} became pull requests; {rows[2]["value"]} were dropped, parked or blocked first'
            elif name == "REVIEW":
                detail = "candidates, at least once, before anything was published"
            elif name == "SHIFTS":
                value = f'{rows[0]["numerator"]} of {rows[0]["denominator"]}'
                detail = f'session summaries kept since {rows[0].get("since") or "—"}; a clean exit is not a finished queue'
            else:
                if rows[0]["note"] != "Recorded suite passed.":
                    raise CommandError("metrics: the supplied suite did not pass; the test fact cannot say passing")
                detail = f'passing in {math.floor(rows[1]["value"] + .5)} s on the private repository; this snapshot ships a subset'
        result[name + '_VALUE'], result[name + '_DETAIL'] = escape(value), escape(detail)
    return result


ASSETS = pathlib.Path(__file__).parent
CSP = ("default-src 'none'; script-src 'sha256-{digest}'; style-src 'unsafe-inline'; img-src 'self' data:; font-src data:; "
       "connect-src 'none'; base-uri 'none'; form-action 'none'")
SLOT = re.compile(r"@@([A-Z0-9_]+)@@")
SERIES = (("opened", "o", "opened"), ("merged", "m", "merged"), ("closed_unmerged", "x", "closed"))
COUNT_WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four"}


def shared():
    """The stylesheet with the display face inlined, the page script, and the policy that admits only that script."""
    css = (ASSETS / "site.css").read_text(encoding="utf-8")
    for weight in ("600", "800"):
        try:
            encoded = (ASSETS / "fonts" / f"bigshoulders-{weight}.woff2.b64").read_text(encoding="ascii").strip()
            usable = base64.b64decode(encoded, validate=True)[:4] == b"wOF2"
        except (OSError, ValueError):
            usable = False
        if usable:
            css = css.replace(f"@@FONT_{weight}@@", encoded)
        else:
            css = re.sub(rf"@font-face\{{[^}}]*@@FONT_{weight}@@[^}}]*\}}\n?", "", css)
    script = (ASSETS / "site.js").read_text(encoding="utf-8").strip()
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    return {"BASE_CSS": css.strip(), "SCRIPT": script, "CSP": CSP.format(digest=digest)}


def fill(name, values):
    return SLOT.sub(lambda match: values[match[1]], (ASSETS / name).read_text(encoding="utf-8"))


def daily(data):
    """The activity projection read defensively: state counts fall back to the ledger metrics, days may be absent."""
    activity = data.get("activity") if isinstance(data.get("activity"), dict) else {}
    totals = activity.get("totals") if isinstance(activity.get("totals"), dict) else {}
    metrics = data["metrics"]
    counts = {name: totals.get(key, metrics["prs_" + name]["value"])
              for name, key in (("opened", "opened"), ("merged", "merged"), ("closed", "closed_unmerged"), ("open", "open"))}
    days = [row for row in activity.get("days") or [] if isinstance(row, dict)]
    return activity, counts, days


def plural(count, word):
    return f"{count:,} {word}" + ("" if count == 1 else "s")


def strip(days):
    """Three rows of daily bars on one scale, so a tall bar means the same count in every row."""
    if not days:
        return '<p class="note">No dated events in this snapshot.</p>'
    peak = max(1, max(row[key] for row in days for key, _, _ in SERIES))
    last = len(days) - 1
    parts = []
    for key, mark, label in SERIES:
        values = [row[key] for row in days]
        cells = []
        for index, row in enumerate(days):
            classes = ' '.join(name for name, on in (("z", not row[key]), ("today", index == last)) if on)
            attribute = f' class="{classes}"' if classes else ''
            cells.append(f'<i{attribute} style="--h:{round(100 * row[key] / peak)};--d:{index}" '
                         f'title="{escape(row["date"])}: {row[key]} {label}"></i>')
        summary = (f'{label.capitalize()} per day from {days[0]["date"]} to {days[last]["date"]}: {sum(values)} in the window, '
                   f'at most {max(values)} in one day.')
        parts.append(f'<div class="strip-lab"><b>{sum(values):,}</b>{label}</div>'
                     f'<div class="days {mark}" role="img" aria-label="{escape(summary)}">{"".join(cells)}</div>')
    ticks = []
    for index, row in enumerate(days):
        date = dt.date.fromisoformat(row["date"])
        ticks.append(f'<span>{date.day} {date:%b}</span>' if index % 7 == 0 else '<span></span>')
    parts.append(f'<div class="axis" aria-hidden="true">{"".join(ticks)}</div>')
    return '\n'.join(parts)


def today_line(data):
    """What the daily counts rest on: the day so far, the totals, when GitHub was last read, and what is missing."""
    activity, counts, days = daily(data)
    zone = activity.get("zone") or "Asia/Singapore"
    dates = opened_dates(data)
    today = activity.get("today") if isinstance(activity.get("today"), dict) else (days[-1] if days else None)
    parts = []
    if today:
        parts.append(f'<b>{escape(today["date"])}, up to the snapshot:</b> {today["opened"]} opened · {today["merged"]} merged · '
                     f'{today["closed_unmerged"]} closed.')
    if all(value is not None for value in counts.values()):
        parts.append(f'<b>Since {escape(dates[0] if dates else "the first pull request")}:</b> {counts["opened"]:,} opened · '
                     f'{counts["merged"]:,} merged · {counts["closed"]:,} closed without merge · {counts["open"]:,} open.')
    observation = activity.get("observation") if isinstance(activity.get("observation"), dict) else {}
    checked = timestamp(observation.get("checked_at"), exact=True)
    stamp = f'{checked.astimezone(ZoneInfo(zone)):%Y-%m-%d %H:%M} ({zone})' if checked else None
    if observation.get("ok") and stamp:
        parts.append(f'Checked against GitHub at {escape(stamp)}.')
    elif stamp:
        parts.append(f'GitHub could not be reached for this snapshot. Counts are as of {escape(stamp)}.')
    else:
        parts.append("This snapshot was not checked against GitHub; counts come from the factory's ledger.")
    missing = activity.get("missing") if isinstance(activity.get("missing"), dict) else {}
    for key, event in (("opened_at", "opening"), ("merged_at", "merge"), ("closed_at", "close")):
        if missing.get(key):
            parts.append(f'{plural(missing[key], "pull request")} with no recorded {event} time: counted in the totals, left out of the days.')
    pairs = (activity.get("replacements") or {}).get("pairs") if isinstance(activity.get("replacements"), dict) else None
    if pairs:
        parts.append(f'{plural(pairs, "replacement pair")} counted once.')
    check = activity.get("reconciliation") if isinstance(activity.get("reconciliation"), dict) else {}
    if check and not check.get("ok"):
        faults = [f'{plural(check[key], word)}' for key, word in (
            ("invalid_rows", "unreadable row"), ("duplicate_rows", "duplicate row"), ("unclassified", "pull request with no recorded outcome"),
            ("invalid_replacements", "replacement pair that does not hold")) if check.get(key)]
        for key, text in (("difference", "between pull requests opened and their outcomes"), ("population_difference", "between this check and the ledger")):
            if check.get(key):
                faults.append(f'a difference of {abs(check[key]):,} {text}')
        parts.append('<b>These daily counts do not reconcile:</b> ' + ('; '.join(faults) if faults else 'the check failed') + '.')
    return ' '.join(parts)


def daily_rows(days):
    return '\n'.join(cells([escape(row["date"])] + [escape(row[key]) for key, _, _ in SERIES], numeric_from=1, date=True)
                     for row in reversed(days))


def rate_block(row, modifier, title, counts, decided_by):
    if row["value"] is None:
        figure = f'<div class="v counts">{escape(count_text(row["numerator"]))} of {escape(count_text(row["denominator"]))}</div>'
        counts = "too few to state a rate"
    else:
        figure = f'<div class="v">{row["value"] * 100:.0f}<sup>%</sup></div>'
    return (f'      <div class="rate{modifier}">{figure}<h3>{escape(title)}</h3><p class="frac">{escape(counts)}</p>'
            f'<p class="by">{escape(decided_by)}</p></div>')


def proof(value, text):
    if value is None:
        return '<span class="none">Not recorded in this snapshot.</span><br>'
    return f'<b>{value:,}</b>{escape(text)}<br>'


def funnel(metrics, opened):
    rows = ((metrics["candidates_found"]["value"], "candidates recorded"),
            (metrics["candidates_stopped_before_publish"]["value"], "stopped before a pull request"),
            (metrics["sent_back_review"]["value"], "sent back by review at least once"), (opened, "pull requests opened"))
    return '\n'.join(f'        <li><b>{value:,}</b>{label}</li>' for value, label in rows if value is not None)


def log_entries(rows, repository, limit=4):
    parts = []
    for row in sorted(rows, key=lambda entry: entry["date"], reverse=True)[:limit]:
        evidence = ' '.join(link(item.get("url") or repository + '/blob/HEAD/' + item["path"], item["label"]) for item in row["links"])
        parts.append(f'    <li><time datetime="{escape(row["date"])}">{escape(row["date"])}</time>'
                     f'<p><span class="lab">What happened</span>{escape(row["happened"])}{" " + evidence if evidence else ""}</p>'
                     f'<p><span class="lab">What changed</span>{escape(row["change"])}</p>'
                     f'<p class="mv"><span class="lab">Meant to move</span><b>{escape(row["moves"])}</b></p></li>')
    return '\n'.join(parts) or '    <li><p>No entries recorded yet.</p></li>'


def evidence_cards(pulls):
    """Selected pull requests from public-evidence.json. A card is shown only while the ledger agrees with what it claims."""
    try:
        chosen = json.loads((ASSETS / "public-evidence.json").read_text(encoding="utf-8"))
        merged, closure = chosen["merged"], chosen.get("closure")
        state = {(row["repository"].lower(), row["number"]): row["bucket"] for row in pulls}
        cards = []
        for item in merged:
            url = f'https://github.com/{item["repo"]}/pull/{item["number"]}'
            if state.get((item["repo"].lower(), item["number"])) != "merged_no_rework" or not PR_URL.fullmatch(url):
                continue
            cards.append(f'    <a class="case" href="{escape(url)}"><span class="chip">Merged</span>'
                         f'<h3>{escape(item["label"])} <small>#{escape(item["number"])}</small></h3><p>{escape(item["text"])}</p>'
                         f'<p class="why"><b>Why this one</b>{escape(item["basis"])}</p></a>')
        shown = len(cards)
        closed = False
        if isinstance(closure, dict):
            url = f'https://github.com/{closure["repo"]}/pull/{closure["number"]}'
            if state.get((closure["repo"].lower(), closure["number"])) == "closed" and PR_URL.fullmatch(url):
                closed = True
                name = f'{closure["repo"].split("/")[1]}#{closure["number"]}'
                cards.append(f'    <article class="case fail"><span class="chip">Closed · {escape(closure["date"])}</span>'
                             f'<blockquote>“{escape(closure["quote"])}”</blockquote>'
                             f'<cite>{escape(closure["quoted"])} <a href="{escape(url)}">{escape(name)}</a></cite>'
                             f'<div class="cols"><p class="why"><b>What happened</b>{escape(closure["happened"])}</p>'
                             f'<p class="why"><b>What changed</b>{escape(closure["changed"])}</p></div></article>')
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return "Selected pull requests", '    <p class="note">No selection is recorded in this snapshot.</p>'
    merges = f'{COUNT_WORDS.get(shown, shown)} merge' + ('' if shown == 1 else 's')
    if shown and closed:
        return merges + " and the closure that changed the rules", '\n'.join(cards)
    if shown:
        return merges + " from the ledger", '\n'.join(cards)
    return ("The closure that changed the rules" if closed else "Selected pull requests",
            '\n'.join(cards) or '    <p class="note">No selection is recorded in this snapshot.</p>')


def page_values(data, config):
    """Values both pages share: identity, links, the snapshot time, the cohort and the daily counts."""
    user = config.get("github_user", "")
    if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", user):
        raise CommandError("metrics: github_user must be a public GitHub login")
    links = public_links(config)
    cohort = data["cohort"]
    dates = opened_dates(data)
    generated = timestamp(data["generated_at"])
    _, _, days = daily(data)
    meter = (f'Of {cohort["n"]} pull requests: {cohort["merged_no_rework"]} merged without rework, '
             f'{cohort["merged_reworked"]} merged after rework, {cohort["closed"]} closed without merge, {cohort["open"]} still open')
    values = {**shared(), "SCHEMA": escape(data["schema_version"]), "UPDATED": escape(data["generated_at"]),
              "SNAPSHOT": escape(generated.strftime('%Y-%m-%d %H:%M UTC') if generated else data["generated_at"]),
              "USER": escape(user), "PROFILE": escape("https://github.com/" + user),
              "FIRST_OPENED": escape(dates[0] if dates else '—'), "METER_LABEL": escape(meter),
              "DEFINITIONS": escape(links["repository"] + '/blob/HEAD/docs/metrics.md'),
              "WINDOW_DAYS": escape(len(days)), "TODAY": today_line(data), "DAILY_ROWS": daily_rows(days)}
    values.update({key.upper(): escape(value) for key, value in links.items()})
    values.update({'COHORT_' + key.upper(): escape(value) for key, value in cohort.items()})
    return user, links, values


def render(data, config=None):
    """The landing page: what the factory is, how a change moves down the line, what it has produced and who built it."""
    user, links, values = page_values(data, CONFIG if config is None else config)
    metrics, cohort, population = data["metrics"], data["cohort"], data["population"]
    activity, counts, days = daily(data)
    merge, clean = metrics["cohort_merge_rate"], metrics["merged_no_rework_share"]
    title, cards = evidence_cards(data["pull_requests"])
    stopped, found = metrics["candidates_stopped_before_publish"]["value"], metrics["candidates_found"]["value"]
    values.update({
        **{key.upper(): escape(count_text(value) if value is None else f"{value:,}") for key, value in counts.items()},
        "PROJECTS": escape(population["projects"]), "MERGED_PROJECTS": escape(population["merged_projects"]),
        "AS_OF_LOCAL": escape(activity.get("as_of_local_date") or (days[-1]["date"] if days else cohort["as_of"])),
        "FUNNEL": funnel(metrics, counts["opened"]),
        "PROOF_SELECT": proof(stopped, f'of {found:,} recorded candidates stopped before a pull request.' if found is not None else 'candidates stopped before a pull request.'),
        "PROOF_RECEIPTS": proof(population.get("command_receipts"), 'signed receipts on record.'),
        "PROOF_REVIEW": proof(metrics["sent_back_review"]["value"], 'candidates sent back at least once before anything shipped.'),
        "PROOF_TESTS": proof(metrics["factory_test_count"]["value"], "tests on the factory's own code."),
        "PROOF_CHANGES": proof(len(data["improvements"]) or None, 'recorded changes, each tied to the failure behind it.'),
        "RATES": '\n'.join((
            rate_block(merge, " merged", "Merge rate",
                       f'{count_text(merge["numerator"])} of {count_text(merge["denominator"])} pull requests opened on or before {cohort["cutoff"]}',
                       "Maintainers decide this. The factory moves it only through what it chooses to work on."),
            rate_block(clean, "", "Merged without rework",
                       f'{count_text(clean["numerator"])} of {count_text(clean["denominator"])} merges with a complete history',
                       "No change request, no fix pushed after opening, no repair by a maintainer. The factory's build and review decide this, "
                       "along with how strictly each project reviews."))),
        "STRIP": strip(days), "EVIDENCE_TITLE": escape(title), "EVIDENCE": cards,
        "LOG": log_entries(data["improvements"], links["repository"]), "IMPROVEMENT_COUNT": escape(len(data["improvements"])),
    })
    return fill("metrics.html", values)


def render_board(data, config=None):
    """The status board: the cohort, the reasons, the days, and every pull request behind the numbers."""
    user, links, values = page_values(data, CONFIG if config is None else config)
    metrics, cohort = data["metrics"], data["cohort"]
    dates = opened_dates(data)
    first = dates[0] if dates else '—'
    generated = timestamp(data["generated_at"])
    last = timestamp(dates[-1]) if dates else None
    query = f'is:pr author:{user} -user:{user} created:{first}..{cohort["cutoff"]}'
    searches = ' · '.join(link('https://github.com/search?' + urlencode({"q": query + suffix, "type": "pullrequests"}), label)
                          for label, suffix in (("opened", ""), ("merged", " is:merged"), ("closed", " is:closed is:unmerged"), ("still open", " is:open")))
    merge, clean = metrics["cohort_merge_rate"], metrics["merged_no_rework_share"]
    neutral = cohort["maintainer_edits_neutral"]
    values.update({
        "TOTAL": escape(metrics["prs_opened"]["value"]),
        "COHORT_FIRST": escape(cohort["first_opened"] or '—'), "COHORT_LAST": escape(cohort["last_opened"] or '—'),
        "MERGE_RATE": escape(rate_text(merge)), "MERGE_DETAIL": escape(rate_detail(merge, merge=True)),
        "CLEAN_RATE": escape(rate_text(clean)), "CLEAN_DETAIL": escape(rate_detail(clean)), "SEARCHES": searches,
        "CLEAN_WIDTH": f'{100 * cohort["merged_no_rework"] / cohort["n"]:.1f}' if cohort["n"] else '0',
        "REWORK_WIDTH": f'{100 * cohort["merged_reworked"] / cohort["n"]:.1f}' if cohort["n"] else '0',
        "CLOSED_WIDTH": f'{100 * cohort["closed"] / cohort["n"]:.1f}' if cohort["n"] else '0',
        "NEUTRAL_EDITS": '<p class="small">' + escape(f'{neutral} merges also carried maintainer edits that were tests, release notes, refactors or extensions. Those are recorded and do not count as rework.') + '</p>' if neutral else '',
        "LAST_OPENED": f'<p class="small">No pull request has been opened since {escape(dates[-1])}.</p>' if generated and last and (generated.date() - last.date()).days > 7 else '',
        "CHART": chart(data["weekly"]["rows"], "Pull requests opened and merged per week", [("opened", "Opened"), ("merged", "Merged")], "count"),
        "WEEKLY_ROWS": weekly_rows(data["weekly"]["rows"], ("opened", "merged")),
        "WEEKLY_OUTCOMES": weekly_rows(data["weekly"]["rows"], ("opened", "cohort_merged", "cohort_no_rework", "cohort_closed", "cohort_open")),
        "IMPROVEMENT_ROWS": improvement_rows(data["improvements"], links["repository"]), "REPOSITORY_ROWS": repository_rows(data["by_repository"], user),
        "PULL_ROWS": pull_request_rows(data["pull_requests"]), "PULL_COUNT": escape(len(data["pull_requests"])), "TABLES": measurement_tables(metrics),
        "COMPARISON": escape(reconciliation_text(data["reconciliation"])),
        "LEDGER_SEARCH": escape('https://github.com/search?' + urlencode({"q": f'is:pr author:{user} -user:{user} created:>={first}', "type": "pullrequests"})),
        "DATA": json.dumps(data, ensure_ascii=True, allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026'),
    })
    values.update({key.upper() + '_REASONS': reason_rows(rows) for key, rows in data["reasons"].items()})
    values.update(fact_values(metrics))
    template = (ASSETS / "board.html").read_text(encoding="utf-8")
    if not data["improvements"]:
        template = re.sub(r'<section class="wrap sec" aria-labelledby="log-title">.*?</section>\n', '', template, flags=re.S)
    return SLOT.sub(lambda match: values[match[1]], template)


CARD_WIDTH = 44


def card_counts(label, days):
    """One status-card line of daily counts. Tighter spacing, then one count per line, keeps any size within the card width."""
    parts = [f"+{sum(row[key] for row in days)} {name}" for key, _, name in SERIES]
    for line in (f"{label:<9}" + "  ".join(parts), f"{label} " + " ".join(parts)):
        if len(line) <= CARD_WIDTH:
            return [line]
    return [label] + ["  " + part for part in parts]


def status_card(data):
    """The public counts as plain text for a README code block, at most 44 columns wide."""
    activity, counts, days = daily(data)
    cohort, metrics = data["cohort"], data["metrics"]
    dates = opened_dates(data)
    date = activity.get("as_of_local_date") or (days[-1]["date"] if days else cohort["as_of"])
    lines = ["software-factory", f"snapshot {date}, Asia/Singapore date", "",
             f"opened  {count_text(counts['opened']):>4}  since {dates[0] if dates else 'n/a'}",
             f"merged  {count_text(counts['merged']):>4}  in {data['population']['merged_projects']} projects",
             f"closed  {count_text(counts['closed']):>4}  without merge",
             f"open    {count_text(counts['open']):>4}", ""]
    for label, key in (("merge rate", "cohort_merge_rate"), ("merged, no rework", "merged_no_rework_share")):
        row = metrics[key]
        pair = f"{count_text(row['numerator'])}/{count_text(row['denominator'])}"
        lines.append(f"{label:<19}{pair:<7} " + (f"{row['value'] * 100:.0f}%" if row["value"] is not None else f"n<{cohort['min_sample']}"))
    lines.append(f"cohort: opened {cohort['maturity_days']}+ days ago (UTC dates)")
    if days:
        lines += ["", *card_counts("today", days[-1:]), *card_counts("7 days", days[-7:])]
    observation = activity.get("observation") if isinstance(activity.get("observation"), dict) else {}
    if not observation.get("ok"):
        checked = timestamp(observation.get("checked_at"), exact=True)
        lines.append(f"not refreshed since {checked.date().isoformat()}" if checked else "not checked against GitHub")
    return "\n".join(line.rstrip() for line in lines) + "\n"


def missing_counts(data, keys=(), cohort_keys=()):
    missing = [key for key in keys if data[key] is None]
    missing += [key for key in cohort_keys if data["cohort"][key] is None]
    return " Counts not measured: " + ', '.join(key.replace('_', ' ') for key in missing) + "." if missing else ""


def percent(numerator, denominator):
    return f"{round(100 * numerator / denominator)}%" if numerator is not None and denominator else "n/a"


def figure_rates(data):
    cohort = data["cohort"]
    n, merged, clean = cohort["n"], cohort["merged"], cohort["merged_no_rework"]
    if "rates" in data:
        return [(rate_text(row), row["numerator"], row["denominator"]) for row in data["rates"]]
    rated = n is not None and n >= cohort["min_sample"]
    return [(percent(a, b) if rated else f"{count_text(a)} of {count_text(b)}", a, b)
            for a, b in ((merged, n), (clean, merged))]


def path_length(points):
    return sum(abs(bx - ax) + abs(by - ay) for (ax, ay), (bx, by) in zip(points, points[1:]))


def token(c, points, offset, end_colour, size=12):
    """One token: fades in, rides its route, takes its final colour near the end, fades out.

    A negative begin puts every token mid-route on the first frame.
    """
    d = "M" + " L".join(f"{x} {y}" for x, y in points)
    seconds = round(path_length(points) / SPEED, 2)
    half, fill = size / 2, c[end_colour]
    timing = f'dur="{seconds}s" begin="-{offset}s" repeatCount="indefinite"'
    return (
        f'<rect class="moving" x="{-half}" y="{-half}" width="{size}" height="{size}" rx="3" fill="{c["flow"]}">'
        f'<animateMotion path="{d}" {timing}/>'
        f'<animate attributeName="opacity" values="0;1;1;0" keyTimes="0;0.04;0.94;1" {timing}/>'
        f'<animate attributeName="fill" values="{c["flow"]};{c["flow"]};{fill};{fill}" keyTimes="0;0.78;0.86;1" {timing}/>'
        "</rect>")


def line(data, theme):
    c = THEMES[theme]
    xs = [FIRST_X + i * PITCH for i in range(len(STATIONS))]
    select, build, verify, gate, deliver, outcome = xs[1], xs[2], xs[3], xs[4], xs[5], xs[6]
    label = line_label(data)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" height="{HEIGHT}" role="img" aria-label="{escape(label)}">',
        f"<title>{escape(label)}</title>",
        "<style>"
        f".mono{{font-family:{MONO}}}.sans{{font-family:{SANS}}}"
        ".lamp{animation:lamp 2.4s ease-in-out infinite}"
        "@keyframes lamp{0%,100%{opacity:.25}50%{opacity:1}}"
        ".belt{animation:belt .7s linear infinite}@keyframes belt{to{stroke-dashoffset:-16}}"
        ".back{animation:back 1.6s linear infinite}@keyframes back{to{stroke-dashoffset:18}}"
        ".still{display:none}"
        "@media (prefers-reduced-motion: reduce){.lamp,.belt,.back{animation:none}.moving{display:none}.still{display:inline}}"
        "</style>",
        f'<defs><pattern id="grid-{theme}" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M24 0H0V24" fill="none" stroke="{c["grid"]}" stroke-width="1"/></pattern>'
        f'<marker id="tip-{theme}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="{c["muted"]}"/></marker></defs>',
        f'<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{HEIGHT - 1}" rx="12" fill="{c["panel"]}" stroke="{c["edge"]}"/>',
        f'<rect x="1" y="1" width="{WIDTH - 2}" height="{HEIGHT - 2}" rx="12" fill="url(#grid-{theme})"/>',
        f'<text class="mono" x="28" y="36" font-size="12" font-weight="700" letter-spacing="1.6" fill="{c["ink"]}">SOFTWARE FACTORY</text>',
        f'<text class="mono" x="176" y="36" font-size="12" letter-spacing="1.6" fill="{c["muted"]}">/ THE LINE</text>',
        f'<text class="mono" x="{WIDTH - 28}" y="36" font-size="12" letter-spacing="1.2" fill="{c["muted"]}" text-anchor="end">SNAPSHOT {escape(data["as_of"])}</text>',
    ]
    # Lessons return to the three stages that can use them.
    out.append(f'<path class="back" d="M{outcome} {BELT_Y - 46}V74H{select}V{BELT_Y - 46}" fill="none" stroke="{c["muted"]}" stroke-width="1.2" stroke-dasharray="3 6"/>')
    for x in (build, verify):
        out.append(f'<path class="back" d="M{x} 74V{BELT_Y - 46}" fill="none" stroke="{c["muted"]}" stroke-width="1.2" stroke-dasharray="3 6"/>')
    out.append(f'<text class="mono" x="{(select + outcome) / 2}" y="65" font-size="11" fill="{c["muted"]}" text-anchor="middle" letter-spacing=".6">OUTCOMES COME BACK AS LESSONS, CHECKS AND REPLAY CASES</text>')
    # Belt, chutes and the sorter after the last station.
    out.append(f'<path d="M24 {BELT_Y}H{SORT_X}" stroke="{c["belt"]}" stroke-width="18" stroke-linecap="round" opacity=".28"/>')
    out.append(f'<path class="belt" d="M24 {BELT_Y}H{SORT_X}" stroke="{c["belt"]}" stroke-width="2" stroke-dasharray="6 10"/>')
    for x in (select, gate):
        out.append(f'<path d="M{x} {BELT_Y + 44}V{BIN_Y - 14}" stroke="{c["belt"]}" stroke-width="2" stroke-dasharray="2 5" marker-end="url(#tip-{theme})"/>')
    out.append(f'<path d="M{verify} {BELT_Y + 44}V{LOOP_Y}H{build}V{BELT_Y + 48}" fill="none" stroke="{c["belt"]}" stroke-width="2" stroke-dasharray="2 5" marker-end="url(#tip-{theme})"/>')
    out.append(f'<path d="M{SORT_X} {ROWS["merged"][0]}V{ROWS["closed"][0]}" stroke="{c["belt"]}" stroke-width="2" stroke-dasharray="2 5"/>')
    for y, _ in ROWS.values():
        out.append(f'<path d="M{SORT_X} {y}H{BOARD_X - 4}" stroke="{c["belt"]}" stroke-width="2"/>')
    # Tokens ride under the housings, so each station briefly hides them.
    entry = (12, BELT_Y)

    def sorted_to(kind):
        y, colour = ROWS[kind]
        return [entry, (SORT_X, BELT_Y), (SORT_X, y), (BOARD_X + 6, y)], colour

    reworked = [entry, (verify, BELT_Y), (verify, LOOP_Y), (build, LOOP_Y), (build, BELT_Y), (SORT_X, BELT_Y),
                (SORT_X, ROWS["merged"][0]), (BOARD_X + 6, ROWS["merged"][0])]
    routes = [
        sorted_to("merged"), ([entry, (select, BELT_Y), (select, BIN_Y)], "off"), sorted_to("merged"),
        (reworked, "good"), sorted_to("closed"), sorted_to("merged"),
        ([entry, (gate, BELT_Y), (gate, BIN_Y)], "off"), sorted_to("merged"), sorted_to("open"),
        ([entry, (select, BELT_Y), (select, BIN_Y)], "off"), sorted_to("merged"),
    ]
    for index, (points, colour) in enumerate(routes):
        out.append(token(c, points, round(0.4 + index * 0.95, 2), colour))
    # A still frame for readers who ask for less motion.
    for x, y, colour in ((xs[0] + 58, BELT_Y, "flow"), (build + 58, BELT_Y, "flow"), (deliver + 58, BELT_Y, "flow"),
                         (select, BIN_Y - 30, "off")):
        out.append(f'<rect class="still" x="{x - 6}" y="{y - 6}" width="12" height="12" rx="3" fill="{c[colour]}"/>')
    # Stations.
    for index, (name, actor) in enumerate(STATIONS):
        x = xs[index]
        outside = index == len(STATIONS) - 1
        dash = ' stroke-dasharray="5 4"' if outside else ""
        lamp = c["good"] if outside else c["flow"]
        out.append(f'<rect x="{x - 46}" y="{BELT_Y - 44}" width="92" height="88" rx="8" fill="{c["house"]}" fill-opacity=".94" stroke="{c["edge"]}" stroke-width="1.2"{dash}/>')
        out.append(f'<path d="M{x - 38} {BELT_Y - 44}H{x + 38}" stroke="{lamp}" stroke-width="2" opacity=".55"/>')
        out.append(f'<circle class="lamp" cx="{x + 33}" cy="{BELT_Y - 29}" r="3.5" fill="{lamp}" style="animation-delay:{index * .34:.2f}s"/>')
        out.append(f'<text class="mono" x="{x - 35}" y="{BELT_Y - 25}" font-size="10" fill="{c["muted"]}">{index + 1:02d}</text>')
        out.append(f'<text class="mono" x="{x}" y="{BELT_Y + 6}" font-size="14" font-weight="700" letter-spacing="1" fill="{c["ink"]}" text-anchor="middle">{name}</text>')
        out.append(f'<text class="sans" x="{x}" y="{BELT_Y + 27}" font-size="10" fill="{c["muted"]}" text-anchor="middle">{escape(actor)}</text>')

    def tag(x, y, title, detail):
        out.append(f'<text class="mono" x="{x}" y="{y}" font-size="11" font-weight="700" letter-spacing="1" fill="{c["muted"]}" text-anchor="middle">{escape(title)}</text>')
        out.append(f'<text class="sans" x="{x}" y="{y + 16}" font-size="11" fill="{c["muted"]}" text-anchor="middle">{escape(detail)}</text>')

    tag(select, BIN_Y + 10, "DROPPED", "claimed, duplicate, no real user")
    tag((build + verify) / 2, LOOP_Y + 22, "SENT BACK", "fix, then review again")
    tag(gate, BIN_Y + 10, "REFUSED", "binding or scan failed")
    for kind, (y, colour) in ROWS.items():
        out.append(f'<rect x="{BOARD_X}" y="{y - 9}" width="18" height="18" rx="4" fill="{c[colour]}" opacity=".9"/>')
        out.append(f'<text class="mono" x="{BOARD_X + 26}" y="{y - 1}" font-size="17" font-weight="700" fill="{c["ink"]}">{escape(count_text(data[kind]))}</text>')
        out.append(f'<text class="mono" x="{BOARD_X + 26}" y="{y + 13}" font-size="9.5" letter-spacing="1" fill="{c["muted"]}">{kind.upper()}</text>')
    # Bottom ledger line.
    base = HEIGHT - 44
    out.append(f'<path d="M28 {base - 20}H{WIDTH - 28}" stroke="{c["edge"]}" stroke-width="1"/>')
    out.append(f'<text class="mono" x="28" y="{base + 2}" font-size="12" fill="{c["ink"]}"><tspan font-weight="700">{escape(count_text(data["candidates"]))}</tspan><tspan fill="{c["muted"]}"> candidates  →  </tspan><tspan font-weight="700">{escape(count_text(data["stopped"]))}</tspan><tspan fill="{c["muted"]}"> stopped inside  →  </tspan><tspan font-weight="700">{escape(count_text(data["opened"]))}</tspan><tspan fill="{c["muted"]}"> pull requests  →  </tspan><tspan font-weight="700" fill="{c["good"]}">{escape(count_text(data["merged"]))}</tspan><tspan fill="{c["muted"]}"> merged</tspan></text>')
    out.append(f'<text class="sans" x="28" y="{base + 22}" font-size="11" fill="{c["muted"]}">Counts are real. The moving tokens only illustrate the routes. The owner sets policy; no person reviews each pull request.</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def board(data, theme):
    c = THEMES[theme]
    cohort = data["cohort"]
    n, merged, clean = cohort["n"], cohort["merged"], cohort["merged_no_rework"]
    reworked, closed, still_open = cohort["merged_reworked"], cohort["closed"], cohort["open"]
    merge_rate, clean_rate = figure_rates(data)
    width, height, pad = 880, 262, 28
    label = (f"Rates on {data['as_of']}. Of {count_text(n)} pull requests opened at least {cohort['maturity_days']} days earlier, "
             f"{count_text(merged)} merged: {count_text(clean)} without rework and {count_text(reworked)} after rework. {count_text(closed)} were closed without "
             f"merge and {count_text(still_open)} are still open.")
    label += missing_counts(data, cohort_keys=("n", "merged", "merged_no_rework", "merged_reworked", "closed", "open"))
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{escape(label)}">',
        f"<title>{escape(label)}</title>",
        f"<style>.mono{{font-family:{MONO}}}.sans{{font-family:{SANS}}}</style>",
        f'<defs><pattern id="b-{theme}" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M24 0H0V24" fill="none" stroke="{c["grid"]}" stroke-width="1"/></pattern></defs>',
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" fill="{c["panel"]}" stroke="{c["edge"]}"/>',
        f'<rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="12" fill="url(#b-{theme})"/>',
        f'<text class="mono" x="{pad}" y="38" font-size="12" font-weight="700" letter-spacing="1.6" fill="{c["ink"]}">SOFTWARE FACTORY</text>',
        f'<text class="mono" x="{pad + 148}" y="38" font-size="12" letter-spacing="1.6" fill="{c["muted"]}">/ RATES</text>',
        f'<text class="mono" x="{width - pad}" y="38" font-size="12" letter-spacing="1.2" fill="{c["muted"]}" text-anchor="end">SNAPSHOT {escape(data["as_of"])}</text>',
    ]
    half = (width - 2 * pad) / 2
    tiles = [
        (merge_rate[0], "MERGE RATE", f"{count_text(merge_rate[1])} of {count_text(merge_rate[2])} opened 14+ days ago",
         "maintainers decide it; selection moves it"),
        (clean_rate[0], "MERGED WITHOUT REWORK", f"{count_text(clean_rate[1])} of {count_text(clean_rate[2])} merges",
         "no change request, fix or maintainer repair"),
    ]
    for index, (value, name, detail, note) in enumerate(tiles):
        x = pad + index * half
        out.append(f'<text class="mono" x="{x:.0f}" y="72" font-size="11" font-weight="700" letter-spacing="1.2" fill="{c["muted"]}">{name}</text>')
        out.append(f'<text class="mono" x="{x:.0f}" y="126" font-size="52" font-weight="700" letter-spacing="-2" fill="{c["ink"]}">{escape(value)}</text>')
        out.append(f'<text class="sans" x="{x + 150:.0f}" y="108" font-size="13" font-weight="600" fill="{c["ink"]}">{escape(detail)}</text>')
        out.append(f'<text class="sans" x="{x + 150:.0f}" y="126" font-size="11.5" fill="{c["muted"]}">{escape(note)}</text>')
    bar_x, bar_y, bar_w, bar_h = pad, 162, width - 2 * pad, 12
    clean_w = bar_w * clean / n if n and clean is not None else 0
    rework_w = bar_w * reworked / n if n and reworked is not None else 0
    closed_w = bar_w * closed / n if n and closed is not None else 0
    out.append(f'<rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="6" fill="{c["track"]}"/>')
    if clean_w:
        out.append(f'<rect x="{bar_x}" y="{bar_y}" width="{clean_w:.1f}" height="{bar_h}" rx="6" fill="{c["fill"]}"/>')
    if rework_w > 2:
        out.append(f'<rect x="{bar_x + clean_w + 2:.1f}" y="{bar_y}" width="{rework_w - 2:.1f}" height="{bar_h}" fill="{c["fill2"]}"/>')
    if closed_w > 2:
        out.append(f'<rect x="{bar_x + clean_w + rework_w + 2:.1f}" y="{bar_y}" width="{closed_w - 2:.1f}" height="{bar_h}" fill="{c["off"]}"/>')
    legend = [(c["fill"], clean, "merged without rework"), (c["fill2"], reworked, "merged after rework"),
              (c["off"], closed, "closed without merge"), (c["track"], still_open, "still open")]
    x = pad
    for colour, count, name in legend:
        out.append(f'<rect x="{x}" y="{bar_y + 30}" width="10" height="10" rx="2" fill="{colour}" stroke="{c["edge"]}" stroke-width="1"/>')
        out.append(f'<text class="sans" x="{x + 16}" y="{bar_y + 39}" font-size="12" fill="{c["ink"]}"><tspan font-weight="700">{escape(count_text(count))}</tspan> {name}</text>')
        x += 46 + 6.6 * (len(count_text(count)) + 1 + len(name))
    out.append(f'<text class="sans" x="{pad}" y="{height - 22}" font-size="11.5" fill="{c["muted"]}">Closed and unanswered pull requests stay in the merge-rate denominator. The 14-day cutoff is a reporting rule, not a deadline.</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def stats(data):
    cohort = data["cohort"]
    merge_rate, clean_rate = figure_rates(data)
    return [
        (count_text(data["merged_total"]), "merged pull requests", f"in {count_text(data['merged_projects'])} projects since {data['since']}"),
        (merge_rate[0], "merge rate",
         f"{count_text(merge_rate[1])} of {count_text(merge_rate[2])} opened 14+ days ago"),
        (clean_rate[0], "merged without rework", f"{count_text(clean_rate[1])} of {count_text(clean_rate[2])} merges"),
    ]


def banner(data, theme, compact=False):
    c = THEMES[theme]
    width, height = (440, 466) if compact else (880, 308)
    pad = 22 if compact else 28
    total = len(STEPS) * STEP_SECONDS
    cohort = data["cohort"]
    label = (f"This account's upstream contributions are run by Software Factory. Snapshot {data['as_of']}: {count_text(data['merged_total'])} merged pull "
             f"requests in {count_text(data['merged_projects'])} projects since {data['since']}. Of {count_text(cohort['n'])} opened at least "
             f"{cohort['maturity_days']} days ago, {count_text(cohort['merged'])} merged, {count_text(cohort['merged_no_rework'])} of them "
             "without rework. Steps: scout, select, build, verify, gate, ship.")
    label += missing_counts(data, ("merged_total", "merged_projects"), ("n", "merged", "merged_no_rework"))
    uid = f"{theme}{'c' if compact else 'w'}"
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{escape(label)}">',
        f"<title>{escape(label)}</title>",
        "<style>"
        f".mono{{font-family:{MONO}}}.sans{{font-family:{SANS}}}"
        f".row{{animation:row {total}s linear infinite;opacity:.42}}"
        f".mark{{animation:mark {total}s linear infinite;opacity:0}}"
        f"@keyframes row{{0%,{100 / len(STEPS):.2f}%{{opacity:1}}{100 / len(STEPS) + 2:.2f}%,100%{{opacity:.42}}}}"
        f"@keyframes mark{{0%,{100 / len(STEPS):.2f}%{{opacity:1}}{100 / len(STEPS) + 2:.2f}%,100%{{opacity:0}}}}"
        ".belt{animation:belt .7s linear infinite}@keyframes belt{to{stroke-dashoffset:-16}}"
        ".blink{animation:blink 1.2s steps(2,start) infinite}@keyframes blink{to{visibility:hidden}}"
        "@media (prefers-reduced-motion: reduce){.row,.mark,.belt,.blink{animation:none}.row{opacity:1}.mark{opacity:1}.moving{display:none}}"
        "</style>",
        f'<defs><pattern id="g-{uid}" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M24 0H0V24" fill="none" stroke="{c["grid"]}" stroke-width="1"/></pattern></defs>',
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" fill="{c["panel"]}" stroke="{c["edge"]}"/>',
        f'<rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="12" fill="url(#g-{uid})"/>',
        f'<circle cx="{pad + 5}" cy="33" r="5" fill="{c["flow"]}"/>',
        f'<text class="mono" x="{pad + 18}" y="38" font-size="13" font-weight="700" letter-spacing="1.4" fill="{c["ink"]}">RUN BY SOFTWARE FACTORY</text>',
    ]
    if compact:
        out.append(f'<text class="sans" x="{pad}" y="60" font-size="12" fill="{c["muted"]}">Agents open the upstream pull requests, issues and replies.</text>')
        out.append(f'<text class="mono" x="{width - pad}" y="38" font-size="10" letter-spacing="1" fill="{c["muted"]}" text-anchor="end">{escape(data["as_of"])}</text>')
    else:
        out.append(f'<text class="mono" x="{width - pad}" y="38" font-size="11.5" letter-spacing="1.2" fill="{c["muted"]}" text-anchor="end">SNAPSHOT {escape(data["as_of"])}</text>')
        out.append(f'<text class="sans" x="{pad}" y="62" font-size="13" fill="{c["muted"]}">An agent pipeline opens this account\'s upstream pull requests, issues and replies. I design it and set its rules.</text>')
    # Numbers.
    numbers = stats(data)
    top = 112 if compact else 124
    column = (width - 2 * pad) / (1 if compact else len(numbers))
    for index, (value, name, detail) in enumerate(numbers):
        x = pad if compact else pad + index * column
        y = top + (index * 62 if compact else 0)
        size = 30 if compact else 40
        out.append(f'<text class="mono" x="{x:.0f}" y="{y}" font-size="{size}" font-weight="700" letter-spacing="-1" fill="{c["good"] if index == 0 else c["ink"]}">{escape(value)}</text>')
        if compact:
            out.append(f'<text class="sans" x="{x + 120:.0f}" y="{y - 12}" font-size="13" font-weight="600" fill="{c["ink"]}">{escape(name)}</text>')
            out.append(f'<text class="sans" x="{x + 120:.0f}" y="{y + 5}" font-size="11.5" fill="{c["muted"]}">{escape(detail)}</text>')
        else:
            out.append(f'<text class="sans" x="{x:.0f}" y="{y + 22}" font-size="13" font-weight="600" fill="{c["ink"]}">{escape(name)}</text>')
            out.append(f'<text class="sans" x="{x:.0f}" y="{y + 39}" font-size="11.5" fill="{c["muted"]}">{escape(detail)}</text>')
    # What the factory does, one step lit at a time.
    first = (top + 3 * 62 - 16) if compact else 192
    row_h = 22 if compact else 21
    columns = 1 if compact else 2
    per_column = len(STEPS) if compact else 3
    out.append(f'<path d="M{pad} {first - 18}H{width - pad}" stroke="{c["edge"]}" stroke-width="1"/>')
    for index, (name, text) in enumerate(STEPS):
        col, line = divmod(index, per_column)
        x = pad + col * (width - 2 * pad) / columns
        y = first + line * row_h + 6
        delay = index * STEP_SECONDS
        out.append(f'<text class="mono mark" x="{x:.0f}" y="{y}" font-size="12" font-weight="700" fill="{c["flow"]}" style="animation-delay:{delay}s">&gt;</text>')
        out.append(f'<g class="row" style="animation-delay:{delay}s"><text class="mono" x="{x + 14:.0f}" y="{y}" font-size="12" font-weight="700" letter-spacing=".6" fill="{c["ink"]}">{name}</text>'
                   f'<text class="sans" x="{x + 70:.0f}" y="{y}" font-size="12" fill="{c["ink"]}">{escape(text)}</text></g>')
    # A short belt along the bottom.
    belt_y = height - 30
    out.append(f'<path d="M{pad} {belt_y}H{width - pad}" stroke="{c["belt"]}" stroke-width="12" stroke-linecap="round" opacity=".25"/>')
    out.append(f'<path class="belt" d="M{pad} {belt_y}H{width - pad}" stroke="{c["belt"]}" stroke-width="2" stroke-dasharray="6 10"/>')
    travel = width - 2 * pad - 12
    seconds = round(travel / 110, 2)
    for k in range(4 if compact else 7):
        offset = round(k * seconds / (4 if compact else 7), 2)
        timing = f'dur="{seconds}s" begin="-{offset}s" repeatCount="indefinite"'
        end = c["good"] if k % 4 else c["belt"]
        out.append(f'<rect class="moving" x="-5" y="-5" width="10" height="10" rx="2.5" fill="{c["flow"]}">'
                   f'<animateMotion path="M{pad + 6} {belt_y}H{pad + 6 + travel}" {timing}/>'
                   f'<animate attributeName="opacity" values="0;1;1;0" keyTimes="0;0.05;0.92;1" {timing}/>'
                   f'<animate attributeName="fill" values="{c["flow"]};{c["flow"]};{end};{end}" keyTimes="0;0.7;0.8;1" {timing}/></rect>')
    out.append("</svg>")
    return "\n".join(out) + "\n"



GATE_FIGURE_WIDTH, GATE_FIGURE_HEIGHT = 960, 420
GATE_CARD_X, GATE_CARD_W, GATE_CARD_H = 28, 250, 70
GATE_X, GATE_W, GATE_TOP, GATE_BOTTOM = 600, 52, 78, 322
GATE_OUT_X, GATE_OUT_W, GATE_FORK_X = 704, 228, 664
GATE_LANES = [
    (118, "INDEPENDENT REVIEW", ("one answer per risk class,", "each citing a receipt"), "HEAD + PR TEXT"),
    (200, "COORDINATOR REVIEW", ("reruns the proofs on this head,", "adds its own counterexample"), "HEAD + PR TEXT"),
    (282, "APPROVAL RECORD", ("the owner's standing approval,", "recorded for this item"), "EXACT HEAD + EXACT TEXT"),
]
GATE_EXITS = [
    (150, "PUSH + PULL REQUEST", ("every binding matches;", "leak and privacy scans pass"), "good"),
    (250, "REFUSED", ("a stale binding, a rerun that exits", "differently, or a scan finding"), "off"),
]
GATE_LABEL = ("The release gate on the structured path. Independent review and coordinator review are each bound to the head "
         "commit and the pull-request text. The approval record is bound to the exact head and the exact prepared text. "
         "The gate compares every binding with what is about to be pushed. When all match and the leak and privacy scans "
         "pass, the push and pull request go out. A stale binding, a rerun that exits differently or a scan finding "
         "refuses the push. A receipt is the CLI's signed record of one proof command.")


def svg_escape(value):
    return escape(value).replace("&#x27;", "'")


def gate_card(out, c, x, y, width, title, lines, lamp):
    top = y - GATE_CARD_H / 2
    out.append(f'<rect x="{x}" y="{top}" width="{width}" height="{GATE_CARD_H}" rx="8" fill="{c["house"]}" fill-opacity=".94" stroke="{c["edge"]}" stroke-width="1.2"/>')
    out.append(f'<path d="M{x + 8} {top}H{x + width - 8}" stroke="{c[lamp]}" stroke-width="2" opacity=".55"/>')
    out.append(f'<text class="mono" x="{x + 14}" y="{top + 25}" font-size="12" font-weight="700" letter-spacing="1" fill="{c["ink"]}">{svg_escape(title)}</text>')
    for index, text in enumerate(lines):
        out.append(f'<text class="sans" x="{x + 14}" y="{top + 43 + index * 15}" font-size="11" fill="{c["muted"]}">{svg_escape(text)}</text>')


def gate(theme):
    c = THEMES[theme]
    left, right = GATE_X - GATE_W / 2, GATE_X + GATE_W / 2
    lane_end = GATE_CARD_X + GATE_CARD_W
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {GATE_FIGURE_WIDTH} {GATE_FIGURE_HEIGHT}" width="{GATE_FIGURE_WIDTH}" height="{GATE_FIGURE_HEIGHT}" role="img" aria-label="{svg_escape(GATE_LABEL)}">',
        f"<title>{svg_escape(GATE_LABEL)}</title>",
        "<style>"
        f".mono{{font-family:{MONO}}}.sans{{font-family:{SANS}}}"
        ".lamp{animation:lamp 2.4s ease-in-out infinite}"
        "@keyframes lamp{0%,100%{opacity:.25}50%{opacity:1}}"
        ".belt{animation:belt .7s linear infinite}@keyframes belt{to{stroke-dashoffset:-16}}"
        ".still{display:none}"
        "@media (prefers-reduced-motion: reduce){.lamp,.belt{animation:none}.moving{display:none}.still{display:inline}}"
        "</style>",
        f'<defs><pattern id="gate-grid-{theme}" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M24 0H0V24" fill="none" stroke="{c["grid"]}" stroke-width="1"/></pattern>'
        f'<marker id="gate-tip-{theme}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="{c["muted"]}"/></marker></defs>',
        f'<rect x="0.5" y="0.5" width="{GATE_FIGURE_WIDTH - 1}" height="{GATE_FIGURE_HEIGHT - 1}" rx="12" fill="{c["panel"]}" stroke="{c["edge"]}"/>',
        f'<rect x="1" y="1" width="{GATE_FIGURE_WIDTH - 2}" height="{GATE_FIGURE_HEIGHT - 2}" rx="12" fill="url(#gate-grid-{theme})"/>',
        f'<text class="mono" x="28" y="36" font-size="12" font-weight="700" letter-spacing="1.6" fill="{c["ink"]}">SOFTWARE FACTORY</text>',
        f'<text class="mono" x="176" y="36" font-size="12" letter-spacing="1.6" fill="{c["muted"]}">/ THE GATE</text>',
        f'<text class="mono" x="{GATE_FIGURE_WIDTH - 28}" y="36" font-size="12" letter-spacing="1.2" fill="{c["muted"]}" text-anchor="end">STRUCTURED PATH, SINCE 2026-11-10</text>',
    ]
    tag_x = (lane_end + left) / 2
    out.append(f'<text class="mono" x="{tag_x}" y="66" font-size="10" letter-spacing="1.2" fill="{c["muted"]}" text-anchor="middle">BOUND TO</text>')
    # Lanes from each record to the gate, with the tokens under the tags and cards.
    for y, _, _, _ in GATE_LANES:
        out.append(f'<path d="M{lane_end} {y}H{left}" stroke="{c["belt"]}" stroke-width="14" stroke-linecap="round" opacity=".22"/>')
        out.append(f'<path class="belt" d="M{lane_end} {y}H{left}" stroke="{c["belt"]}" stroke-width="2" stroke-dasharray="6 10"/>')
    for index, (y, _, _, _) in enumerate(GATE_LANES):
        for step in range(2):
            out.append(token(c, [(lane_end, y), (left + 4, y)], round(0.3 + index * 0.55 + step * 1.25, 2), "flow", size=10))
        out.append(f'<rect class="still" x="{tag_x + 86}" y="{y - 5}" width="10" height="10" rx="3" fill="{c["flow"]}"/>')
    # Exits.
    push_y, refuse_y = GATE_EXITS[0][0], GATE_EXITS[1][0]
    middle = GATE_LANES[1][0]
    out.append(f'<path d="M{right} {middle}H{GATE_FORK_X}V{push_y}H{GATE_OUT_X - 4}" fill="none" stroke="{c["belt"]}" stroke-width="2" marker-end="url(#gate-tip-{theme})"/>')
    out.append(f'<path d="M{GATE_FORK_X} {middle}V{refuse_y}H{GATE_OUT_X - 4}" fill="none" stroke="{c["belt"]}" stroke-width="2" stroke-dasharray="2 5" marker-end="url(#gate-tip-{theme})"/>')
    for step in range(2):
        out.append(token(c, [(right, middle), (GATE_FORK_X, middle), (GATE_FORK_X, push_y), (GATE_OUT_X - 2, push_y)], round(0.2 + step * 0.52, 2), "good", size=10))
    out.append(f'<rect class="still" x="{GATE_FORK_X - 5}" y="{push_y - 5}" width="10" height="10" rx="3" fill="{c["good"]}"/>')
    # Binding tags sit on the lanes.
    for y, _, _, binding in GATE_LANES:
        width = len(binding) * 7.1 + 24
        out.append(f'<rect x="{tag_x - width / 2}" y="{y - 11}" width="{width}" height="22" rx="11" fill="{c["panel"]}" stroke="{c["edge"]}"/>')
        out.append(f'<text class="mono" x="{tag_x}" y="{y + 3.5}" font-size="10" letter-spacing="1" fill="{c["ink"]}" text-anchor="middle">{svg_escape(binding)}</text>')
    # Records.
    for y, title, lines, _ in GATE_LANES:
        gate_card(out, c, GATE_CARD_X, y, GATE_CARD_W, title, lines, "flow")
    for y, title, lines, lamp in GATE_EXITS:
        gate_card(out, c, GATE_OUT_X, y, GATE_OUT_W, title, lines, lamp)
        out.append(f'<circle cx="{GATE_OUT_X + GATE_OUT_W - 16}" cy="{y - GATE_CARD_H / 2 + 20}" r="3.5" fill="{c[lamp]}"/>')
    # The gate.
    out.append(f'<text class="mono" x="{GATE_X}" y="66" font-size="12" font-weight="700" letter-spacing="1.4" fill="{c["ink"]}" text-anchor="middle">RELEASE GATE</text>')
    out.append(f'<rect x="{left}" y="{GATE_TOP}" width="{GATE_W}" height="{GATE_BOTTOM - GATE_TOP}" rx="10" fill="{c["house"]}" fill-opacity=".96" stroke="{c["edge"]}" stroke-width="1.4"/>')
    out.append(f'<path d="M{left + 8} {GATE_TOP}H{right - 8}" stroke="{c["flow"]}" stroke-width="2" opacity=".7"/>')
    out.append(f'<path d="M{GATE_X} {GATE_TOP + 14}V{GATE_BOTTOM - 14}" stroke="{c["edge"]}" stroke-width="1" stroke-dasharray="2 4"/>')
    for index, (y, _, _, _) in enumerate(GATE_LANES):
        out.append(f'<circle cx="{GATE_X}" cy="{y}" r="9" fill="{c["panel"]}" stroke="{c["edge"]}"/>')
        out.append(f'<circle class="lamp" cx="{GATE_X}" cy="{y}" r="4.5" fill="{c["flow"]}" style="animation-delay:{index * .5:.1f}s"/>')
    out.append(f'<text class="sans" x="{GATE_X}" y="{GATE_BOTTOM + 21}" font-size="11" fill="{c["muted"]}" text-anchor="middle">every binding is compared with what is about to be pushed</text>')
    # Bottom ledger line.
    base = GATE_FIGURE_HEIGHT - 36
    out.append(f'<path d="M28 {base - 22}H{GATE_FIGURE_WIDTH - 28}" stroke="{c["edge"]}" stroke-width="1"/>')
    out.append(f'<text class="mono" x="28" y="{base}" font-size="12" fill="{c["ink"]}"><tspan font-weight="700">receipt</tspan><tspan fill="{c["muted"]}"> = the CLI\'s signed record of one proof command: argv · exit status · output hash · commits</tspan></text>')
    out.append(f'<text class="sans" x="28" y="{base + 20}" font-size="11" fill="{c["muted"]}">The gate checks bindings and scans. It does not judge whether a reviewer was right.</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


ACTIVITY_WIDTH, ACTIVITY_HEIGHT = 880, 176
ACTIVITY_ROWS = (("opened", "flow", "OPENED"), ("merged", "good", "MERGED"), ("closed_unmerged", "off", "CLOSED"))


def activity(data, theme):
    """Daily events for the last window, three rows on one scale, dated by Asia/Singapore day."""
    c = THEMES[theme]
    projection, _, days = daily(data)
    width, height, pad = ACTIVITY_WIDTH, ACTIVITY_HEIGHT, 28
    date = projection.get("as_of_local_date") or (days[-1]["date"] if days else data["cohort"]["as_of"])
    sums = {key: sum(row[key] for row in days) for key, _, _ in ACTIVITY_ROWS}
    if days:
        label = (f"Daily activity from {days[0]['date']} to {days[-1]['date']}, Asia/Singapore dates: {sums['opened']} pull requests opened, "
                 f"{sums['merged']} merged and {sums['closed_unmerged']} closed without merge. Each event is dated by when it happened.")
    else:
        label = "Daily activity: no dated events in this snapshot."
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{escape(label)}">',
        f"<title>{escape(label)}</title>",
        f"<style>.mono{{font-family:{MONO}}}.sans{{font-family:{SANS}}}"
        ".bar{transform-box:fill-box;transform-origin:50% 100%;animation:rise .9s cubic-bezier(.2,.7,.2,1) both}"
        "@keyframes rise{from{transform:scaleY(0)}to{transform:scaleY(1)}}"
        "@media (prefers-reduced-motion:reduce){.bar{animation:none}}</style>",
        f'<defs><pattern id="a-{theme}" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M24 0H0V24" fill="none" stroke="{c["grid"]}" stroke-width="1"/></pattern></defs>',
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" fill="{c["panel"]}" stroke="{c["edge"]}"/>',
        f'<rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="12" fill="url(#a-{theme})"/>',
        f'<text class="mono" x="{pad}" y="36" font-size="12" font-weight="700" letter-spacing="1.6" fill="{c["ink"]}">LAST {len(days)} DAYS</text>',
        f'<text class="mono" x="{pad + 118}" y="36" font-size="12" letter-spacing="1.6" fill="{c["muted"]}">/ BY DAY</text>',
        f'<text class="mono" x="{width - pad}" y="36" font-size="12" letter-spacing="1.2" fill="{c["muted"]}" text-anchor="end">ASIA/SINGAPORE DATES · SNAPSHOT {escape(date)}</text>',
    ]
    left, right, top, row_height, bar_height = 156, width - pad, 52, 34, 26
    peak = max([1] + [row[key] for row in days for key, _, _ in ACTIVITY_ROWS])
    step = (right - left) / max(1, len(days))
    bar_width = max(2, step - 4)
    for index, (key, colour, name) in enumerate(ACTIVITY_ROWS):
        base = top + index * row_height + bar_height
        out.append(f'<text class="mono" x="{pad}" y="{base - 3}" font-size="20" font-weight="700" fill="{c["ink"]}">{sums[key]}</text>')
        out.append(f'<text class="mono" x="{pad + 12.4 * len(str(sums[key])) + 8:.0f}" y="{base - 4}" font-size="10.5" letter-spacing="1.2" fill="{c["muted"]}">{name}</text>')
        out.append(f'<path d="M{left} {base + .5}H{right}" stroke="{c["edge"]}" stroke-width="1"/>')
        for position, row in enumerate(days):
            x = left + position * step + 2
            if row[key]:
                size = max(3, bar_height * row[key] / peak)
                out.append(f'<rect class="bar" x="{x:.1f}" y="{base - size:.1f}" width="{bar_width:.1f}" height="{size:.1f}" fill="{c[colour]}" style="animation-delay:{position * 18}ms"/>')
            else:
                out.append(f'<rect x="{x:.1f}" y="{base - 2}" width="{bar_width:.1f}" height="2" fill="{c["track"]}"/>')
    if days:
        today_x = left + (len(days) - 1) * step
        out.append(f'<rect x="{today_x:.1f}" y="{top - 4}" width="{step:.1f}" height="{3 * row_height + 2}" fill="none" stroke="{c["muted"]}" stroke-width="1" stroke-dasharray="3 3"/>')
        axis = top + 3 * row_height + 12
        for position, row in enumerate(days):
            if position % 7 == 0:
                day = dt.date.fromisoformat(row["date"])
                out.append(f'<text class="mono" x="{left + position * step + 2:.1f}" y="{axis}" font-size="10" fill="{c["muted"]}">{day.day} {day:%b}</text>')
        out.append(f'<text class="mono" x="{right}" y="{axis}" font-size="10" fill="{c["muted"]}" text-anchor="end">snapshot day</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def figures(data):
    values = figure_data(data)
    return {**{f"{name}-{theme}.svg": draw(values, theme) for name, draw in (("line", line), ("board", board))
               for theme in ("light", "dark")},
            **{f"profile-{theme}{'-compact' if compact else ''}.svg": banner(values, theme, compact)
               for theme in ("light", "dark") for compact in (False, True)},
            **{f"gate-{theme}.svg": gate(theme) for theme in ("light", "dark")},
            **{f"activity-{theme}.svg": activity(data, theme) for theme in ("light", "dark")}}
