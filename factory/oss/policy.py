"""Read a repo's contribution rules and surface the ones that change what we may do.

Heuristic by design: it prints the matching lines with their file so a person or agent reads
the actual rule. The gate verifies issue preconditions against GitHub rather than trusting flags in item.json.
"""
import base64
import re

from factory.oss import community
from factory.util import CommandError, gh_json, iso

VERSION = 12

FILES = [
    "CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md", "CONTRIBUTING.rst",
    "contributing.md", ".github/contributing.md", "docs/contributing.md", ".github/commit-convention.md",
    "AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md", "AI_POLICY.md", ".github/AI_POLICY.md",
    "AI-POLICY.md", "docs/AI_POLICY.md", "AI.md", ".github/AI.md", "docs/AI.md", "AI_POLICY.rst", ".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE.md",
    "PULL_REQUEST_TEMPLATE.md", "docs/pull_request_template.md", "CODE_OF_CONDUCT.md",
    ".github/CODE_OF_CONDUCT.md", "docs/CODE_OF_CONDUCT.md",
]

WORKFLOW_RULES = {
    "linked_issue_enforced": re.compile(
        r"closed automatically|closing keyword|(?:does ?n[o']t|not) link(?:ed)? to (?:any |an )?(?:existing )?issue|not assigned to you",
        re.I,
    ),
}

ISSUE_REQUIREMENTS = {
    "linked_issue_closing_keyword": re.compile(r"\bclosing[- ]keywords?\b", re.I),
    "linked_issue_assignment": re.compile(r"\bassign(?:ed|ment|ees?)?\b", re.I),
}

NO_AI_ATTRIBUTION = re.compile(r"\b(do not|don't|never|no)\b[^.\n]{0,40}\b(include|add|use)?\b[^.\n]{0,20}\bAI[- ]?(attribution|co-?author|footer|signature)", re.I)

REPRO_FIELD = re.compile(r"reproduc|\brepro\b|stackblitz|codesandbox|minimal example", re.I)

RULES = {
    "collaborators_only": re.compile(
        r"(pull requests|PRs)[^.\n]{0,40}\b(limited|restricted)\s+to[^.\n]{0,40}\b(collaborators|maintainers|team members|core team)"
        r"|(do not|don't|won't|will not|no longer)\s+accept\w*\s+(pull requests|PRs|code contributions)\s+from\s+(non-collaborators|outside|external|the community)"
        r"|not accepting\s+(external|outside|community)\s+(pull requests|PRs|contributions)"
        r"|disabled\s+(pull requests|PRs)[^\n]{0,200}?\bfor\s+(outside|external)\s+contributors"
        r"|\bnot\s+accepting\s+(\*\*)?(contributions|pull requests|PRs)\b"
        r"|\b(pull requests|PRs)\s+are\s+(turned off|disabled|closed)\s+for\s+this\s+repo",
        re.I,
    ),
    "ai_disclosure": re.compile(
        r"\b(disclos|declar|indicat|mention|state|note|label)\w*[^.\n]{0,80}\b(ai|llm|copilot|chatgpt|claude|cursor|codex|generated|tool)\b"
        r"|From <Tool|Assisted-by|Generated-by|AI[- ]assist|automated agent|ai usage",
        re.I,
    ),
    "claim_first": re.compile(
        r"\b(assign|claim|approv|discuss|open an issue)\w*[^.\n]{0,80}\b(before|first|prior to)\b"
        r"|\bbefore (you )?(start|open|submit)\w*[^.\n]{0,60}\b(issue|discuss|assign)",
        re.I,
    ),
    "human_prose": re.compile(
        r"\b(descriptions?|comments?|prose|communications?|issues?|pull requests?)\b[^.\n]{0,80}\b(must|should|prefer)\b[^.\n]{0,40}\b(human[- ](?:written|authored)|written by (?:a )?human|yourself|your own words|by you)\b"
        r"|\b(do not|don't|never)\s+use\s+(ai|llms?)\s+to\s+write"
        r"|\bprefer\b[^.\n]{0,40}\b(descriptions?|comments?)\b[^.\n]{0,60}\byourself\b"
        r"|\bnever let an llm speak for you\b",
        re.I,
    ),
    "cla": re.compile(
        r"\bCLA\b|Contributor License Agreement|\bDCO\b|Developer Certificate of Origin|Signed-off-by"
        r"|\bsign[- ]?off\b[^.\n]{0,30}\bcommits?\b|\bcommits?\b[^.\n]{0,30}\bsigned[- ]off\b|git commit -s\b",
        re.I,
    ),
    "changeset": re.compile(r"\bchangesets?\b|pnpm changeset|npx changeset", re.I),
    "tests_required": re.compile(r"\b(add|include|write)\w*[^.\n]{0,40}\btests?\b", re.I),
    "screenshots": re.compile(r"\b(screenshot|screen recording|gif|video|demo)\w*", re.I),
    "no_trivial": re.compile(r"\b(typo|cosmetic|trivial|drive-by)\w*[^.\n]{0,80}\b(not|won't|will not|reject|close)", re.I),
}


AI = r"(?:(?<![A-Za-z0-9])AI(?![A-Za-z0-9])|\bLLMs?\b|artificial intelligence|large language models?|ChatGPT|Copilot|Claude|Codex|Cursor|automated (?:agents?|contributions?|pull requests?))"
AI_BAN = re.compile(
    rf"\b(?:do not|don't|won't|will not|never|not)\s+(?:accept|allow|merge|review|submit|use)\b[^.\n]{{0,140}}{AI}"
    rf"|\bno\s+(?:{AI}|(?:part|portion|use|usage|contributions?|code)\b[^.\n]{{0,140}}{AI})"
    rf"|\b(?:ban\w*|prohibit\w*|forbid\w*|reject\w*|declin\w*|refus\w*|refrain from)\b[^.\n]{{0,140}}{AI}"
    rf"|{AI}[^.\n]{{0,140}}\b(?:not (?:currently )?(?:accepted|allowed|permitted|welcome|used)|do not accept|don't accept|banned|prohibited|forbidden|will be closed|rejected|declined)\b"
    rf"|\b(?:have not|did not|haven't|didn't|does not|do not|not|never)\s+(?:used?|generated|written|contain|include)[^.\n]{{0,80}}{AI}", re.I)
AI_RESTRICTION = re.compile(
    rf"{AI}[^.\n]{{0,180}}\b(?:discouraged|restricted|prior (?:approval|permission)|explicit (?:approval|permission)|only (?:for|to|if|when|be used|used)|must (?:obtain|get|seek|be approved)|human (?:oversight|review)|reviewed by (?:a )?human)\b"
    rf"|\b(?:discourage\w*|restrict\w*|prior approval|explicit permission|human review)[^.\n]{{0,140}}{AI}"
    rf"|{AI}[^.\n]{{0,100}}\b(?:requires?|needs?)[^.\n]{{0,40}}\b(?:approval|permission)\b", re.I)
FORMAT = re.compile(r"last line|final line|checkbox|section|following format|exact(?:ly)? (?:text|wording)|trailer|footer|`|\b(?:model|version|prompts?|tools?|extent|scope)\b", re.I)
DISCLOSE = re.compile(
    r"^(?:[-*] )?(?:please )?(?:disclos\w*|declar\w*|indicat\w*|mention|state|include|write|use|add|label|specify|list)\b"
    r"|\b(?:must|should|required to)\s+(?:be |clearly )?(?:disclos\w*|declar\w*|indicat\w*|mention|state|include|write|use|add|label|specify|list)\b"
    r"|\b(?:last|final) line[^.]*\bmust\b", re.I)
SECTION = re.compile(r"#{1,6}\s+(?:AI|LLM|automation)(?:[- ]assistance)?\s+(?:usage|use|disclosure|declaration)|#{1,6}\s+AI assistance", re.I)


def restrictive_text(text):
    """Remove explicit permissions, not adjacent restrictions in the same rule."""
    text = text.replace("’", "'")
    text = re.sub(r"\bno restrictions? (?:on|for)\b", "permit", text, flags=re.I)
    text = re.sub(r"\b(?:(?:do|does|did|will|must|should) not|don't|doesn't|won't|never)\s+"
                  r"(?:ban\w*|prohibit\w*|forbid\w*|reject\w*|declin\w*|refus\w*|discourag\w*|restrict\w*)\b",
                  "permit", text, flags=re.I)
    text = re.sub(r"\bnot\s+(?:banned|prohibited|forbidden|rejected|declined|discouraged|restricted)\b",
                  "permitted", text, flags=re.I)
    text = re.sub(r"\b(?:(?:do|does) not|don't|doesn't|never)\s+(?:require|need)\s+"
                  r"(?:(?:any|prior|explicit|human|maintainer)\s+)*(?:approval|permission|review)\b",
                  "permit", text, flags=re.I)
    text = re.sub(r"\b(?:require\w*|need\w*)\s+no\s+(?:prior\s+)?(?:approval|permission)\b"
                  r"|\bno\s+(?:prior\s+)?(?:approval|permission)\s+is\s+required\b",
                  "permit", text, flags=re.I)
    # Understanding, testing and disclosure are contributor responsibilities, not
    # limits on which tasks automation may perform. Other conditions stay blocked.
    neutral = set("you your their its the this a an all every any code changes contribution contributions "
                  "contributor contributors they it use usage is are be was have has been and or must should "
                  "understand understood review reviewed test tested tests testing take responsibility for "
                  "disclose disclosed declare declared clearly fully in pr pull request description".split())

    def condition(match):
        words = set(re.findall(r"[a-z]+", match[1].lower()))
        if words <= neutral and words & {"understand", "review", "test", "tested", "disclosed", "disclose", "declared"}:
            return ""
        return match[0]

    return re.sub(r"\bonly (?:if|when)\s+([^.;\n]+)", condition, text, flags=re.I)


def automation_rules(texts):
    """Quote restrictions and bind each supported format to its own instruction.

    Only simple generic declarations are renderable. A heading or checkbox never
    satisfies a separate instruction to supply models, prompts or other details.
    """
    flags, formats = {}, []
    for path, text in texts.items():
        heading = ""
        for match in re.finditer(r"(?m)^#{1,6} [^\n]*|^[ \t]*[-*] \[[ xX]\][^\n]*|"
                                 r"[^\n]+(?:\n(?!\s*\n|#{1,6} |[ \t]*[-*] \[)[^\n]+)*", text):
            paragraph = match[0]
            n = text.count("\n", 0, match.start()) + 1
            flat = " ".join(paragraph.split())
            if re.match(r"#{1,6} ", flat):
                heading = flat
            context = (heading + ": " if re.search(AI, heading, re.I) and heading != flat else "") + flat
            quote, source = f"{path}:{n}: {context}", f"{path}:{n}"
            if NO_AI_ATTRIBUTION.search(context):
                flags.setdefault("no_ai_attribution", []).append(quote)
            candidate = restrictive_text(context)
            for key, pattern in (("ai_ban", AI_BAN), ("ai_restriction", AI_RESTRICTION)):
                clause = NO_AI_ATTRIBUTION.sub("", candidate) if key == "ai_ban" else candidate
                if pattern.search(clause):
                    flags.setdefault(key, []).append(quote)
            if not re.search(AI, context, re.I):
                continue
            if re.match(r"[-*] \[[ xX]\] ", flat):
                formats.append({"kind": "checkbox", "text": flat, "source": source})
                continue
            if "pull_request_template" in path.lower() and SECTION.fullmatch(flat):
                formats.append({"kind": "section", "text": flat, "source": source})
                continue
            for clause in re.split(r";|(?<=[.!?])\s+", flat):
                if not FORMAT.search(clause) or not DISCLOSE.search(clause.strip().replace("**", "")):
                    continue
                # Quoted forbidden examples are not affirmative instructions.
                if re.search(r"\b(?:do not|don't|never)\b", clause, re.I):
                    continue
                rule = f"{source}: {clause}"
                flags.setdefault("ai_disclosure_format", []).append(rule)
                if re.search(r"<[^>]+>|\{[^}]+\}|\b(?:model|version|prompts?|tool)\b", clause, re.I):
                    continue
                for literal in re.findall(r"`([^`\n]+)`|[\"“]([^\"”\n]+)[\"”]", clause):
                    value = next(v for v in literal if v)
                    if re.search(AI, value, re.I):
                        formats.append({"kind": "section" if value.startswith("#") else "literal",
                                        "text": value, "source": source, "rule": rule})
    # Keep distinct source-rule bindings, including repeated identical wording.
    unique = {(row["kind"], row["text"], row.get("rule")): row for row in formats}
    return flags, list(unique.values())


def blockers(rules):
    flags = rules.get("flags", {})
    found = [quote for key in ("ai_ban", "ai_restriction", "human_prose") for quote in flags.get(key, [])]
    for quote in flags.get("no_ai_attribution", []):
        text = re.split(r":\d+: ", quote, maxsplit=1)[-1]
        commit_only = re.search(r"\bcommits?\b", text, re.I) and not re.search(
            r"\bPRs?\b|pull requests?|descriptions?|issues?|comments?|docs|release notes", text, re.I)
        if not commit_only:
            found.append(quote)
    return list(dict.fromkeys(found))


def fetch(repo, path):
    try:
        data = gh_json("api", f"repos/{repo}/contents/{path}")
    except CommandError as error:
        if re.search(r"\b404\b", str(error)):
            return None
        raise
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        return None
    return base64.b64decode(data["content"]).decode("utf-8", "replace")


def required_repro(text):
    """Labels of issue-form fields that ask for a reproduction and cannot be left empty."""
    labels = []
    for block in re.split(r"(?m)^\s*-\s*type:", text)[1:]:
        names = re.findall(r"(?m)^\s*(?:label|id):\s*(.+)$", block)
        if any(REPRO_FIELD.search(n) for n in names) and re.search(r"(?m)^\s*required:\s*true\b", block):
            labels.append(names[-1].strip().strip("'\""))
    return labels


DOCS_ONLY_CASE = re.compile(r"^\s*((?:['\"]?[\w.*/-]+['\"]?\|)*['\"]?[\w.*/-]*(?:docs|\.md)[\w.*/-]*['\"]?(?:\|['\"]?[\w.*/-]+['\"]?)*)\)\s*;;", re.M)


def assignment_author_exemption(text):
    """Recognize the shell guard's issue-author condition, not comments about exemptions."""
    if not re.search(r"(?m)^\s*ISSUE_AUTHOR=\$\([^\n]*jq\s+-r\s+['\"]\.user\.login['\"]\)", text):
        return None
    clause = re.search(
        r'(?m)^(?P<indent>[ \t]*)if \[ "\$PR_AUTHOR" != "\$ISSUE_AUTHOR" \]'
        r'(?:\s*&&\s*\[\[ "\$PR_AUTHOR" != \*"\[bot\]" \]\])?; then\n'
        r'(?P<body>.*?)^(?P=indent)fi\b', text, re.S,
    )
    assignments = list(re.finditer(r"\.assignees\b", text))
    if clause and assignments and all(clause.start("body") <= m.start() < clause.end("body") for m in assignments):
        if re.search(r'gh\s+pr\s+close\b', clause["body"]):
            return text.count("\n", 0, clause.start()) + 1, clause[0].splitlines()[0].strip()
    return None


def run(repo):
    found, flags = {}, {k: [] for k in RULES}
    templates = []
    try:
        listing = gh_json("api", f"repos/{repo}/contents/.github/PULL_REQUEST_TEMPLATE") or []
        templates = [f".github/PULL_REQUEST_TEMPLATE/{f['name']}" for f in listing if f.get("type") == "file"]
    except CommandError:
        pass
    texts = {path: text for path in FILES + templates if (text := fetch(repo, path)) is not None}
    texts.update(community.resolve(repo, gh_json))
    automation, formats = automation_rules(texts)
    flags.update(automation)
    for path, text in texts.items():
        if "/ISSUE_TEMPLATE/" in path and path.endswith((".yml", ".yaml")):
            for label in required_repro(text):
                flags.setdefault("repro_required", []).append(f"{path}: required field '{label}'")
        found[path] = len(text.splitlines())
        for n, line in enumerate(text.splitlines(), 1):
            for key, rx in RULES.items():
                if rx.search(line):
                    if key == "ai_disclosure" and NO_AI_ATTRIBUTION.search(line):
                        key = "no_ai_attribution"
                    flags.setdefault(key, []).append(f"{path}:{n}: {line.strip()[:200]}")
    try:
        workflows = [w["path"] for w in gh_json("api", f"repos/{repo}/contents/.github/workflows") or []
                     if w.get("type") == "file" and w["name"].endswith((".yml", ".yaml"))]
    except CommandError:
        workflows = []
    assignment_without_exemption = False
    for path in workflows:
        text = fetch(repo, path)
        if text is None:
            continue
        rules = dict(WORKFLOW_RULES)
        if WORKFLOW_RULES["linked_issue_enforced"].search(text):
            rules.update(ISSUE_REQUIREMENTS)
            docs = DOCS_ONLY_CASE.search(text)
            if docs and re.search(r"docs[_-]only", text, re.I):
                globs = [g.strip("'\"") for g in docs.group(1).split("|")]
                flags.setdefault("linked_issue_docs_exempt", []).append(f"{path}: docs-only globs={','.join(globs)}")
            if ISSUE_REQUIREMENTS["linked_issue_assignment"].search(text):
                exemption = assignment_author_exemption(text)
                if exemption:
                    n, clause = exemption
                    flags.setdefault("linked_issue_assignment_author_exempt", []).append(f"{path}:{n}: {clause}")
                else:
                    assignment_without_exemption = True
        for n, line in enumerate(text.splitlines(), 1):
            for key, rx in rules.items():
                if rx.search(line):
                    flags.setdefault(key, []).append(f"{path}:{n}: {line.strip()[:200]}")
    if assignment_without_exemption:
        flags.pop("linked_issue_assignment_author_exempt", None)
    try:
        forms = [f["path"] for f in gh_json("api", f"repos/{repo}/contents/.github/ISSUE_TEMPLATE") or []
                 if f.get("type") == "file" and f["name"].endswith((".yml", ".yaml")) and f["name"] != "config.yml"]
    except CommandError:
        forms = []
    for path in forms:
        if path in texts:
            continue
        text = fetch(repo, path)
        for label in required_repro(text or ""):
            flags.setdefault("repro_required", []).append(f"{path}: required field '{label}'")
    meta = gh_json("api", f"repos/{repo}")
    return {
        "repo": repo, "version": VERSION, "at": iso(), "files": found, "default_branch": meta["default_branch"],
        "stars": meta["stargazers_count"], "archived": meta["archived"], "license": (meta.get("license") or {}).get("spdx_id"),
        "flags": {k: list(dict.fromkeys(v)) for k, v in flags.items() if v},
        "disclosure_formats": formats,
    }
