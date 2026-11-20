from bisect import bisect_right
import getpass
import hashlib
import ipaddress
import pathlib
import re
import socket
from urllib.parse import urlsplit

from factory.util import CONFIG


def name_variants(term):
    words = term.split()
    if 1 < len(words) <= 4:
        return list(dict.fromkeys((term, " ".join(reversed(words)), "".join(words), "".join(reversed(words)))))
    return [term] if term else []


SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "AWS access key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b"), "GitHub token"),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}"), "OpenAI/Anthropic key"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key"),
    (re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}\b"), "Stripe live key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "JWT"),
    (re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@'\"]+:[^/\s:@'\"]+@[^\s'\"<>`]*", re.I), "credentials in URL"),
    (re.compile(r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d)\b[\"']?\s*[:=]\s*[\"']([A-Za-z0-9+/_\-]{24,})[\"']"), "credential assignment"),
]


def identity_markers():
    """This machine's home directory, login and host name, plus private_terms from the local config
    (employer, internal domains, work accounts). Generic example paths such as /home/user are fine."""
    markers = [str(pathlib.Path.home()), getpass.getuser(), socket.gethostname().split(".")[0]]
    markers += [variant for term in CONFIG.get("private_terms", []) for variant in name_variants(term)]
    return [m for m in dict.fromkeys(markers) if len(m) >= 4 and m.lower() not in ("localhost", "root", "user")]


CLOUDFLARE_ID = re.compile(r"\b(?:account[_-]?id|zone[_-]?id|database[_-]?id|bucket(?:[_-]?(?:id|name))?|id)[\"']?\s*[:=]\s*[\"']?([a-f0-9]{32})(?![a-f0-9])", re.I)
CLOUDFLARE_HOST = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:workers|pages)\.dev\b", re.I)
OAUTH = re.compile(r"\b(?:[a-z0-9]+[_-])*(?:oauth)?client[_-]?(?:id|secret)[\"']?\s*[:=]\s*(?:(?P<quote>[\"'`])(?P<quoted>(?:\\.|(?!(?P=quote))[^\\\r\n])*)(?P=quote)|(?P<token>[a-z0-9][a-z0-9_-]{15,})(?![\w.-])(?=\s*(?:[,;}#]|$)))", re.I | re.M)
OAUTH_PLACEHOLDER = re.compile(r"your|example|fixture|fake|dummy|test|xxx|<", re.I)
EMAIL = re.compile(r"(?<![\w.+-])[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?:\.[a-z]{2,}|(?<=localhost))\b", re.I)
PHONE = re.compile(r"(?<![\w.+-])(?:\+\d[\d ().-]{6,}\d|\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}|\d{4}[ .-]\d{4})(?![\w-]|\.\d)")
IP = re.compile(r"(?<![\w:])::ffff:(?:\d{1,3}\.){3}\d{1,3}(?![\w.])|(?<![\w:])(?:\d{1,3}\.){3}\d{1,3}(?![\w.]|\.\d)|(?<![\w:])(?:[a-f0-9]{0,4}:){2,7}[a-f0-9]{0,4}(?![\w:])", re.I)
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "fc00::/7", "fe80::/10"))
INTERNAL_HOST = re.compile(r"(?<![\w.])(?:[a-z0-9_-]+\.)+(?:internal|local|lan|corp|intranet|home)(?![\w.-])", re.I)
URL = re.compile(r"https?://[^\s<>\"'`\\]+", re.I)
MCP_TOKEN = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|//[^\n]*|/\*.*?\*/|\#[^\n]*|[\w$]+|[{}:=]''', re.S)
LARK = re.compile(r"https?://(?:[a-z0-9-]+\.)*(?:feishu\.cn|larksuite\.com|larkoffice\.com)/(?:docx?|wiki|sheets|base|mindnotes)/[^\s<>\"'`]+", re.I)


def placeholder_host(host):
    host = host.rstrip(".")
    if any(label.isupper() or "your" in label.lower() or "example" in label.lower() for label in host.split(".")):
        return True
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in ("example.com", "example.org", "example.net", "invalid", "test", "example", "localhost"))


def mcp_url_offsets(text):
    if "mcpservers" not in text.lower():
        return set()
    offsets, stack = set(), []
    key = previous = ""
    for match in MCP_TOKEN.finditer(text):
        token = match.group(0)
        if token.startswith(("//", "/*", "#")):
            continue
        if token == "{":
            stack.append(bool(stack and stack[-1]) or (key == "mcpservers" and previous in (":", "=")))
        elif token == "}":
            if stack:
                stack.pop()
        elif stack and stack[-1] and key == "url" and previous in (":", "=") and token[0] in "\"'`":
            url = URL.match(token, 1)
            if url:
                offsets.add(match.start() + url.start())
        key, previous = previous.strip("\"'`").lower(), token
    return offsets


def term_pattern(term):
    return r"\s+".join(re.escape(word) for word in term.split())


def matches(text, path="", *, prose=True, markers=None):
    for marker in (identity_markers() if markers is None else markers):
        for m in re.finditer(rf"(?<![\w-]){term_pattern(marker)}(?![\w])", text, re.I):
            yield m.start(), m.end(), "local identity", m.group(0)
    for i, term in enumerate(CONFIG.get("private_terms", []), 1):
        for variant in name_variants(term):
            for m in re.finditer(term_pattern(variant), text, re.I):
                yield m.start(), m.end(), f"private term #{i}", m.group(0)
    for rx, why in SECRET_PATTERNS + LOCAL_PATTERNS:
        for m in rx.finditer(text):
            yield m.start(), m.end(), why, m.group(1) if m.groups() else m.group(0)
    for m in HOME_PATH.finditer(text):
        if m.group(1).lower() not in GENERIC_USERS and not m.group(1).startswith("<"):
            yield m.start(), m.end(), "local home path", m.group(0)
    for i, slug in enumerate(CONFIG.get("private_repos", []), 1):
        if slug:
            for m in re.finditer(rf"(?<![\w.-]){re.escape(slug)}(?:\.git)?(?![\w.-])", text, re.I):
                yield m.start(), m.end(), f"private repo #{i}", m.group(0)
    for rx, why in ((CLOUDFLARE_ID, "Cloudflare id"), (INTERNAL_HOST, "internal hostname"), (LARK, "Lark/Feishu document link")):
        for m in rx.finditer(text):
            yield m.start(), m.end(), why, next((value for value in m.groups() if value is not None), m.group(0))
    for m in OAUTH.finditer(text):
        value = m.group("quoted")
        if value is not None:
            if len(value.strip()) < 8 or OAUTH_PLACEHOLDER.search(value) or (m.group("quote") == "`" and "${" in value):
                continue
        else:
            value = m.group("token")
            if not any(char.isdigit() for char in value):
                continue
        yield m.start(), m.end(), "OAuth client assignment", value
    for m in CLOUDFLARE_HOST.finditer(text):
        if not placeholder_host(m.group(0)):
            yield m.start(), m.end(), "Cloudflare deployment hostname", m.group(0)
    for m in EMAIL.finditer(text):
        value = m.group(0)
        if not safe_email(value):
            yield m.start(), m.end(), "email address", value
    for m in PHONE.finditer(text) if prose else ():
        if 8 <= len(re.sub(r"\D", "", m.group(0))) <= 15:
            yield m.start(), m.end(), "phone number", m.group(0)
    if prose:
        for m in re.finditer(r"(?:\b(?:phone|mobile|tel(?:ephone)?|call|contact)(?:\s+(?:number|me|at|is))*|手机号|电话)\s*[:=]?\s*[\"']?(\d{8,15})\b", text, re.I):
            yield m.start(), m.end(), "phone number", m.group(1)
    for m in IP.finditer(text):
        try:
            address = ipaddress.ip_address(m.group(0))
        except ValueError:
            continue
        address = getattr(address, "ipv4_mapped", None) or address
        if any(address in network for network in PRIVATE_NETWORKS if address.version == network.version):
            yield m.start(), m.end(), "private IP", m.group(0)
    mcp_urls = mcp_url_offsets(text)
    for m in URL.finditer(text):
        try:
            parsed = urlsplit(m.group(0))
            host = parsed.hostname or ""
        except ValueError:
            continue
        authority = parsed.netloc.rsplit("@", 1)[-1]
        original_host = authority[1:].partition("]")[0] if authority.startswith("[") else authority.partition(":")[0]
        if not host or placeholder_host(original_host) or not re.fullmatch(r"[a-z0-9.:-]+", host):
            continue
        if "/mcp" in parsed.path.lower() or "/sse" in parsed.path.lower() or m.start() in mcp_urls:
            yield m.start(), m.end(), "MCP server URL", m.group(0)
        if "." not in host and ":" not in host and not re.search(r"[{}$]", host):
            yield m.start(), m.end(), "internal hostname", m.group(0)


GENERIC_USERS = {"alice", "bob", "user", "me", "you", "runner", "username", "example", "test", "ubuntu", "root"}
HOME_PATH = re.compile(r"/(?:Users|home)/([^/\s\"'`]+)/?", re.I)
LOCAL_PATTERNS = [
    (re.compile(r"~/(?:Documents|Desktop|Downloads|Library|Pictures|Movies|Music|\.claude|\.codex|\.ssh|\.aws|\.config/gh)(?:/[^\s\"'`<>]*)?\b", re.I), "personal path"),
    (re.compile(r"(?:/private)?/tmp/claude-[^\s\"'`<>]*|/var/folders/[^\s\"'`<>]*|\.claude/jobs/[^\s\"'`<>]*|\bcc-daemon\b", re.I), "agent temporary path"),
    (re.compile(
        r"\b(?:set-cookie|cookie)\s*[\"']?\s*:"
        r"(?![ \t]*[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*"
        r"(?:[ \t]*\([ \t]*(?:[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*[ \t]*)?\))?"
        r"(?:(?<=\))[ \t]*(?=[\r\n]|$)"
        r"|[ \t]*,[ \t]*(?=(?:[A-Za-z_$][\w$]*|[\"'][\w$-]+[\"'])[ \t]*:|[\r\n]|$)"
        r"|[ \t]*(?:,[ \t]*)?\}[ \t}\]);,]*(?=[\r\n]|$)))"
        r"\s*[\"']?[^\r\n\"'`]+", re.I), "cookie header"),
    (re.compile(r"\bauthorization\s*[\"']?\s*:\s*[\"']?\s*(?:bearer|basic)\s+[^\s\"'`]+", re.I), "authorization header"),
    (re.compile(r"\b(?:session(?:[_-]?(?:id|key|token))?|sid|connect\.sid|JSESSIONID|PHPSESSID|csrftoken|auth_token|access_token|refresh_token)\b[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9%+/_=.:-]{16,})", re.I), "session cookie"),
    (re.compile(r"(?<![\w.-])id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?(?![\w.-]|\.pub\b)", re.I), "SSH private key filename"),
]


def safe_email(value):
    domain = value.rsplit("@", 1)[-1].lower()
    return value.lower() == "noreply@github.com" or domain in {"users.noreply.github.com", "noreply.github.com"} or any(
        domain == d or domain.endswith("." + d)
        for d in ("example.com", "example.org", "example.net", "test", "invalid", "example", "localhost")
    )


def reviewed_source_line(source, category, reviewed):
    """Match trusted file-scoped rules, never rules supplied by outgoing content."""
    if category.startswith("private term #"):
        category = "private reference"
    digest = hashlib.sha256(source.encode()).hexdigest()
    return any(row["category"] == category and row["sha256"] == digest and row["reason"].strip()
               for row in reviewed)


def masked(value, category):
    return value[:3] + "…" + category


def redact(text):
    spans = sorted((start, end) for start, end, _, _ in matches(text))
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        text = text[:start] + "[redacted]" + text[end:]
    return text


def findings(label, text, allow=(), *, prose=False, markers=None, waive=()):
    found = []
    newlines = [m.start() for m in re.finditer("\n", text)]
    for start, end, why, value in matches(text, prose=prose, markers=markers):
        if value in allow or text[start:end] in allow:
            continue
        line = bisect_right(newlines, start - 1) + 1
        if (line, why) in waive:
            continue
        found.append(f"{redact(label)}:{line}: possible private {why}: {masked(value, why)}")
    return list(dict.fromkeys(found))
