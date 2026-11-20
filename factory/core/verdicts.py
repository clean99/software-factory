"""The only readers of review outcomes.

Legacy items keep verdicts in Markdown (verify.md, review.md, coordinator-review.md); schema-2
items keep the independent and the coordinator review in JSON. Everything that needs a verdict,
the commit it was given for, or the PR text it covered asks here.
"""
import hashlib
import re

from factory.util import git, read_json

VERDICTS = ("ship", "fix-needed", "drop")
VALUE_VERDICTS = ("build", "propose", "drop")
VERDICT = re.compile(r"^\s*verdict\s*:\s*\**\s*(ship|fix-needed|drop)\b", re.I | re.M)
REVIEWER = re.compile(r"^\s*reviewer\s*:\s*(.+)$", re.I | re.M)
REVIEWED_SHA = re.compile(r"^\s*head\s*:\s*([0-9a-f]{7,40})\b", re.I | re.M)


class EvidenceError(ValueError):
    """An evidence file exists but cannot be read; the message names the file and the fix."""


def schema(item):
    return int(item.meta.get("schema") or 1)


def demand_sha256(item):
    """Hash the demand bytes reviewed before implementation."""
    path = item.file("demand.json")
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def pr_sha256(item):
    """Hash of pr.md exactly as it is; approvals bind to this."""
    path = item.file("pr.md")
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def pr_text_sha256(item):
    """Hash of pr.md with the reference to its own issue normalised to #ISSUE.

    Reviews of an issue-first item are written while pr.md still says #ISSUE; opening the issue
    replaces the placeholder with the number. That edit alone must not void the reviews, any
    other change to the text must.
    """
    path = item.file("pr.md")
    if not path.exists():
        return None
    text = path.read_text()
    meta = item.meta
    number = meta.get("issue")
    if meta.get("issue_first") == "issue" and str(number or "").isdigit():
        repo = re.escape(str(meta.get("repo") or ""))
        text = re.sub(rf"https://github\.com/{repo}/issues/{number}(?![\w/])", "#ISSUE", text, flags=re.I)
        text = re.sub(rf"(?<![\w/])#{number}(?!\d)", "#ISSUE", text)
    return hashlib.sha256(text.encode()).hexdigest()


def markdown_text(path):
    try:
        return path.read_text() if path.exists() else None
    except (OSError, UnicodeDecodeError) as error:
        raise EvidenceError(f"{path.name} cannot be read ({error}); restore readable Markdown and re-review") from None


def _markdown(path):
    text = markdown_text(path)
    if text is None:
        return None
    verdict = VERDICT.search(text)
    head = REVIEWED_SHA.search(text)
    reviewer = REVIEWER.search(text)
    reviewer_name = reviewer[1].strip() if reviewer else ""
    return {
        "verdict": verdict.group(1).lower() if verdict else None,
        "head": head[1].lower() if head else None,
        "reviewer": reviewer_name or None,
        "pr_sha256": None,
        "source": path.name,
    }


def _load(path):
    try:
        data = read_json(path)
    except (ValueError, OSError) as error:
        raise EvidenceError(f"{path.name} cannot be read ({error}); rewrite it as one valid JSON object") from None
    if data is not None and not isinstance(data, dict):
        raise EvidenceError(f"{path.name} must hold one JSON object; rewrite it")
    return data


def _json(path):
    data = _load(path)
    if data is None:
        return None
    verdict = str(data.get("verdict") or "").lower()
    return {
        "verdict": verdict if verdict in VERDICTS else None,
        "head": str(data.get("head") or "").lower() or None,
        "reviewer": str(data.get("reviewer") or "").strip() or None,
        "pr_sha256": data.get("pr_sha256") or None,
        "source": path.name,
    }


def read_self_review(item):
    return _markdown(item.file("verify.md"))


def read_review(item):
    if schema(item) >= 2:
        return _json(item.file("review.json"))
    return _markdown(item.file("review.md"))


def read_coordinator(item):
    if schema(item) >= 2:
        return _json(item.file("coordinator-review.json"))
    return _markdown(item.file("coordinator-review.md"))


def read_value_review(item):
    """The pre-build judgement of whether the change is worth a maintainer's time (schema-2 items only)."""
    data = _load(item.file("value-review.json"))
    if data is None:
        return None
    verdict = str(data.get("verdict") or "").lower()
    return {"verdict": verdict if verdict in VALUE_VERDICTS else None, "reviewer": str(data.get("reviewer") or "").strip() or None,
            "demand_sha256": data.get("demand_sha256") or None, "source": "value-review.json"}


def recommitted(clone, sha, head):
    """True when head only re-commits sha: same tree and message (e.g. to run the machine's hooks, L-035)."""
    tree = git(clone, "rev-parse", f"{sha}^{{tree}}", check=False)
    msg = git(clone, "log", "-1", "--format=%B", sha, check=False)
    return bool(tree) and tree == git(clone, "rev-parse", f"{head}^{{tree}}") and msg == git(clone, "log", "-1", "--format=%B", head)


def bound(item, head, sha):
    """Does a verdict given for sha still cover head: the same commit, or an identical re-commit of it."""
    if not sha:
        return False
    if head.startswith(sha):
        return True
    from factory.core import clones

    if clones.released(item) is not None:
        return False
    return recommitted(item.clone, sha, head)
