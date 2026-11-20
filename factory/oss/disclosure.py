"""Deterministic disclosure for new PR bodies; never changes replies or old PRs."""
import datetime as dt
import re

from factory.oss import policy
from factory.util import CONFIG, now, parse_iso, public_destinations


GENERIC_USE = re.compile(
    r"(?:I|we) (?:used|have used) (?:AI|LLM)(?: assistance| tools?)?"
    r"(?: (?:for|to prepare) (?:this|the) (?:contribution|pull request|PR|change))?"
    r"|(?:this (?:contribution|pull request|PR|change) (?:was |is )?)?"
    r"(?:generated|prepared|assisted) (?:with|by) (?:AI|an? LLM)"
    r"|AI (?:used|assisted)|AI[- ](?:generated|assisted)", re.I)


def generic_attestation(text):
    statement = re.sub(r"^[-*] \[[ xX]\] ", "", text).strip("_*. ")
    return bool(GENERIC_USE.fullmatch(statement))


def existing_pr_body(body, rules, created_at):
    """Use GitHub's creation time, not today's date or mutable item metadata."""
    try:
        at = parse_iso(created_at)
        if at.tzinfo is None:
            raise ValueError("missing timezone")
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("PR creation date is required to preserve disclosure on body edits") from error
    return new_pr_body(body, rules, at=at)


def default_line():
    repo = "https://github.com/" + public_destinations(CONFIG)[0]
    return (f"Prepared by an automated contribution pipeline that I build and operate ({repo}); "
            "the tests and evidence above were produced by it.")


def new_pr_body(body, rules, *, at=None):
    """Add the factory-owned text after review without rewriting the approved draft.

    Explicit upstream formats take precedence. Unrecognized instructions to use a
    format block shipping rather than silently substituting our own wording.
    """
    if (at or now()).astimezone(dt.timezone.utc).date().isoformat() < CONFIG["public"]["disclosure_since"]:
        return body
    blocked = policy.blockers(rules)
    if blocked:
        raise ValueError("automation policy blocks submission: " + "; ".join(blocked))
    formats = rules.get("disclosure_formats", [])
    flags = rules.get("flags", {})
    resolved = {spec.get("rule") for spec in formats}
    unresolved = [rule for rule in flags.get("ai_disclosure_format", []) if rule not in resolved]
    if unresolved:
        raise ValueError("unsupported disclosure format; read the quoted rule: " + "; ".join(unresolved))
    if not formats:
        line = default_line()
        return body if line in body.splitlines() else body.rstrip() + "\n\n" + line + "\n"

    if any(re.search(r"\bhuman\b|\bpersonally\b|\bmanually\b", spec["text"], re.I) for spec in formats):
        raise ValueError("disclosure format requires a human attestation: " +
                         "; ".join(spec["text"] for spec in formats))
    for spec in formats:
        if spec["kind"] in {"checkbox", "literal"} and not generic_attestation(spec["text"]):
            raise ValueError("unsupported disclosure attestation; provenance cannot establish this statement: " + spec["text"])
        if spec["kind"] == "section" and not policy.SECTION.fullmatch(spec["text"]):
            raise ValueError("unsupported disclosure format: " + spec["text"])
    repo = "https://github.com/" + public_destinations(CONFIG)[0]
    if not any(spec["kind"] == "section" for spec in formats) and repo not in body:
        body = body.rstrip() + f"\n\nContribution pipeline: {repo}\n"
    literals = {spec["text"] for spec in formats if spec["kind"] == "literal"}
    if len(literals) > 1:
        raise ValueError("conflicting disclosure footer formats: " + "; ".join(sorted(literals)))
    # Literal footers must remain last when a template also has a section.
    formats = sorted(formats, key=lambda spec: spec["kind"] == "literal")
    for spec in formats:
        kind, text = spec["kind"], spec["text"]
        if kind == "checkbox":
            checked = re.sub(r"\[[ xX]\]", "[x]", text, count=1)
            pattern = re.escape(text).replace(r"\[\ \]", r"\[[ xX]\]")
            body, count = re.subn("(?m)^" + pattern + "$", lambda _: checked, body)
            if not count and checked not in body.splitlines():
                body = body.rstrip() + "\n\n" + checked + "\n"
        elif kind == "section":
            heading = re.escape(text)
            match = re.search("(?m)^" + heading + r"\s*$", body)
            if match:
                following = body[match.end():]
                end = re.search(r"(?m)^#{1,6} ", following)
                section = following[:end.start()] if end else following
                if default_line() not in section:
                    body = body[:match.end()] + "\n\n" + default_line() + "\n" + following
            else:
                body = body.rstrip() + "\n\n" + text + "\n\n" + default_line() + "\n"
        elif kind == "literal":
            # A prescribed final line stays final, including on a second render.
            body = "\n".join(line for line in body.splitlines() if line != text).rstrip()
            body += "\n\n" + text + "\n"
        else:
            raise ValueError(f"unsupported disclosure format: {kind}")
    return body
