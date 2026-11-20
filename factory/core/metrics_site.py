"""Static portfolio rendering; metric computation stays in metrics.py."""
import html
import json
import math
import pathlib
import re

from factory.core.metrics import GROUPS


def escape(value):
    return html.escape(str(value), quote=True)


def value_text(value, unit):
    if value is None:
        return "Not tracked"
    if unit == "percent":
        return f"{value * 100:.1f}%"
    if unit == "hours":
        return f"{value / 24:.1f} d" if value >= 48 else f"{value:.1f} h"
    if unit in ("minutes", "seconds", "rounds", "calls"):
        return f"{value:,.1f} {unit}" if isinstance(value, float) else f"{value:,} {unit}"
    return f"{value:,}"


def table(headers, rows):
    return '<div class="table-scroll"><table><thead><tr>' + ''.join(f'<th scope="col">{escape(h)}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join(
        '<tr>' + ''.join(f'<td>{cell}</td>' for cell in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def date_ticks(count):
    """Keep endpoints and at most one middle date so labels fit a phone plot."""
    intervals = min(2, count - 1)
    return {round(i * (count - 1) / intervals) for i in range(intervals + 1)} if intervals > 0 else {0}


def chart(rows, title, series, unit, *, bars=False):
    """All geometry uses one zero-based scale; absent observations leave gaps."""
    values = [row[key] for row in rows for key, _ in series if row[key] is not None]
    if not rows or not values:
        return '<p class="empty">No dated observations yet.</p>'
    peak = max(1, max(values))
    tick_step = 10 ** math.floor(math.log10(peak)) / 2
    maximum = 1 if unit == "percent" else math.ceil(peak / tick_step) * tick_step
    # 16-unit text stays above 12px in a 254px-wide phone card.
    left, top, width, height = 48, 22, 208, 148
    step = width / max(len(rows), 1)

    def x(index):
        return left + (index + .5) * step

    def y(value):
        return top + height * (1 - value / maximum)

    end_labels = {s: y(rows[-1][key]) - 4 for s, (key, _) in enumerate(series) if rows[-1][key] is not None}
    if len(end_labels) == 2:
        upper, lower = sorted(end_labels, key=end_labels.get)
        if end_labels[lower] - end_labels[upper] < 20:
            end_labels[upper] = max(top + 10, min(end_labels[upper] - 10, top + height - 22))
            end_labels[lower] = end_labels[upper] + 20
    parts = [f'<svg viewBox="0 0 320 220" role="img" aria-label="{escape(title)}; exact values in the table below">',
             f'<title>{escape(title)}</title>']
    if bars:
        parts.append('<defs><pattern id="bar-texture" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
                     '<path d="M0 0V6" class="texture-line"/></pattern></defs>')
    for tick in (0, maximum / 2, maximum):
        parts.append(f'<path class="grid-line" d="M{left} {y(tick):.2f}H{left + width}"/>')
        label = f"{tick * 100:g}%" if unit == "percent" else f"{tick:g}"
        parts.append(f'<text class="axis" x="{left - 7}" y="{y(tick) + 4:.2f}" text-anchor="end">{escape(label)}</text>')
    if not bars:
        for s, (key, _) in enumerate(series):
            segment = []
            for i, row in enumerate(rows + [{key: None}]):
                if row[key] is None:
                    if segment:
                        parts.append(f'<path class="series-{s + 1} trend" d="M' + ' L'.join(segment) + '"/>')
                        segment = []
                else:
                    segment.append(f'{x(i):.2f} {y(row[key]):.2f}')
    for i, row in enumerate(rows):
        tip = row["week"] + ": " + "; ".join(f"{label} {value_text(row[key], unit)}" for key, label in series)
        parts.append(f'<g class="mark-group" tabindex="0" data-tip="{escape(tip)}"><title>{escape(tip)}</title>')
        parts.append(f'<path class="crosshair" d="M{x(i):.2f} {top}V{top + height}"/>')
        if bars:
            bar_width = min(12, max(1, (step - 6) / len(series)))
            for s, (key, label) in enumerate(series):
                value = row[key]
                if value is None:
                    continue
                bx = x(i) + (s - (len(series) - 1) / 2) * (bar_width + 2) - bar_width / 2
                by = y(value)
                h = top + height - by
                # Round only the data end, not the baseline.
                radius = min(4, bar_width / 2, h / 2)
                d = (f'M{bx:.2f} {top + height}V{by + radius:.2f}Q{bx:.2f} {by:.2f} {bx + radius:.2f} {by:.2f}'
                     f'H{bx + bar_width - radius:.2f}Q{bx + bar_width:.2f} {by:.2f} {bx + bar_width:.2f} {by + radius:.2f}V{top + height}Z')
                parts.append(f'<path class="series-{s + 1} bar" d="{d}"/>')
                if s == 1:
                    parts.append(f'<path class="texture" d="{d}" fill="url(#bar-texture)"/>')
                if i == len(rows) - 1:
                    label_y = end_labels[s]
                    parts.append(f'<path class="label-leader" d="M{bx + bar_width:.2f} {by:.2f}L{left + width + 4} {label_y - 4:.2f}"/>')
                    parts.append(f'<text class="direct-label" x="{left + width + 6}" y="{label_y:.2f}">{escape(label)}</text>')
        else:
            for s, (key, _) in enumerate(series):
                if row[key] is not None:
                    parts.append(f'<circle class="series-{s + 1} point" cx="{x(i):.2f}" cy="{y(row[key]):.2f}" r="4"/>')
        parts.append(f'<rect class="hit-area" x="{x(i) - max(step / 2, 12):.2f}" y="{top}" width="{max(step, 24):.2f}" height="{height}"/>')
        parts.append('</g>')
        if i in date_ticks(len(rows)):
            parts.append(f'<text class="axis date-axis" x="{x(i):.2f}" y="{top + height + 24}" text-anchor="middle">{row["week"][5:]}</text>')
    parts.append('</svg>')
    if len(series) > 1:
        legend = '<div class="legend">' + ''.join(f'<span><i class="swatch series-{s + 1}"></i>{escape(label)}</span>' for s, (_, label) in enumerate(series)) + '</div>'
    else:
        unit_label = {"hours": "Hours", "percent": "Percent of concluded PRs"}.get(unit, unit)
        legend = f'<div class="chart-unit">{escape(unit_label)}</div>'
    tab = table(["Week (UTC)"] + [label for _, label in series], [[escape(r["week"])] + [escape(value_text(r[key], unit)) for key, _ in series] for r in rows])
    return legend + ''.join(parts) + '<div class="tooltip" hidden></div><details><summary>View weekly values</summary>' + tab + '</details>'


def render(data):
    metrics = data["metrics"]
    highlights = []
    for key in ("first_round_approval_share", "prs_merged", "merge_rate", "prs_open"):
        row = metrics[key]
        coverage = row["coverage"]
        reason = f'<small>{escape(row["reason"])}</small>' if row.get("reason") else ''
        state = ' class="untracked"' if row["value"] is None else ''
        highlights.append(f'<div class="stat"><span class="stat-label">{escape(row["label"])}</span>'
                          f'<strong{state}>{escape(value_text(row["value"], row["unit"]))}</strong>'
                          f'<small>{coverage["covered"]}/{coverage["total"]} records with evidence</small>{reason}</div>')
    sections = []
    for group in GROUPS:
        rows = []
        for row in metrics.values():
            if row["group"] != group:
                continue
            coverage = row["coverage"]
            detail = escape(row["definition"])
            for field in ("reason", "note"):
                if row.get(field):
                    detail += '<p class="detail-note">' + escape(row[field]) + '</p>'
            rows.append([f'<span class="metric-name">{escape(row["label"])}</span>',
                         f'<strong>{escape(value_text(row["value"], row["unit"]))}</strong><span class="status">{row["status"].replace("_", " ")}</span>',
                         detail, f'<span class="coverage">{coverage["covered"]}/{coverage["total"]}</span><small>{escape(row["source"])}</small>'])
        sections.append(f'<section class="metrics-section" id="{group.lower().replace(" ", "-")}"><h2>{escape(group)}</h2>'
                        + table(["Metric", "Recorded value", "Definition & limits", "Coverage & source"], rows) + '</section>')
    rows = data["weekly"]["rows"]
    charts = []
    for title, series, unit, bars, note in (
        ("Weekly output", [("opened", "Opened"), ("merged", "Merged")], "count", True, "PRs by submission date and merge date. These are separate event cohorts."),
        ("Cohort acceptance", [("merge_rate", "Merge rate")], "percent", False, "Merged / concluded, grouped by submission week. Open PRs are excluded; recent cohorts are unfinished."),
        ("Candidate-to-PR lead time", [("median_lead_hours", "Median hours")], "hours", False, "Median hours from candidate creation to submission, by submission week. Missing timestamp pairs leave gaps, not zeroes."),
    ):
        charts.append(f'<section class="chart"><h3>{escape(title)}</h3><p class="chart-note">{escape(note)}</p>' + chart(rows, title, series, unit, bars=bars) + '</section>')
    coverage = data["weekly"]["coverage"]
    weekly_table = table(["Week (UTC)", "Opened", "Merged", "Currently open¹", "Concluded cohort²", "Cohort merge rate", "Closed share", "First-round approval", "Median lead", "Lead samples", "Request to fix", "Owner interventions"],
                         [[escape(r["week"]), str(r["opened"]), str(r["merged"]), str(r["open_submission_cohort"]), str(r["cohort_concluded"]),
                           escape(value_text(r["merge_rate"], "percent")), escape(value_text(r["closed_unmerged_share"], "percent")),
                           escape(value_text(r["first_round_approval_share"], "percent")),
                           escape(value_text(r["median_lead_hours"], "hours")), str(r["lead_time_samples"]),
                           escape(value_text(r["review_fix_hours"], "hours")), escape(value_text(r["owner_interventions"], "count"))] for r in rows])
    template = pathlib.Path(__file__).with_name("metrics.html").read_text(encoding="utf-8")
    replacements = {"STATS": ''.join(highlights), "CHARTS": ''.join(charts), "TABLES": ''.join(sections), "WEEKLY_TABLE": weekly_table,
                    "SCHEMA": str(data["schema_version"]), "UPDATED": escape(data["generated_at"]), "METHODOLOGY": ''.join('<li>' + escape(text) + '</li>' for text in data["methodology"]),
                    "DATE_COVERAGE": '; '.join(f'{key.replace("_", " ")}: {v["covered"]}/{v["total"]}' for key, v in coverage.items()),
                    "INPUT_QUALITY": escape(', '.join(f'{key.replace("_", " ")}: {value}' for key, value in data["input_quality"].items())),
                    "UNTRACKED": str(sum(row["status"] == "not_tracked" for row in metrics.values())),
                    "DATA": json.dumps(data, ensure_ascii=True, allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')}
    replacements.update({key.upper(): escape(value) for key, value in data["links"].items()})
    return re.sub(r"@@([A-Z_]+)@@", lambda match: replacements[match[1]], template)
