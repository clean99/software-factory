"""Diff and prose hygiene checks.

Each rule encodes a concrete reason maintainers reject or distrust a PR. Hard rules fail the
gate; soft rules warn and fail only when they pile up.
"""
import io
import re
import tokenize

from factory.core import privacy, published
from factory.core.privacy import SECRET_PATTERNS, identity_markers
from factory.util import CONFIG, git, run

TEST_PATH = re.compile(
    r"(^|/)(__tests__|__mocks__|tests?|spec|e2e|fixtures?)/|\.(test|spec|stories)\.[a-z]+$|_test\.(go|py)$|(^|/)test_[^/]*\.py$"
)
DOC_PATH = re.compile(r"\.(md|mdx|rst|txt)$|(^|/)(docs?|\.changeset)/")
SNAPSHOT_PATH = re.compile(r"__snapshots__/|\.snap$")
GENERATED_PATH = re.compile(r"(^|/)(generated|__generated__)/|\.generated\.[a-z]+$|\.gen\.[a-z]+$")


def is_generated(path, meta):
    import fnmatch

    return bool(GENERATED_PATH.search(path)) or any(fnmatch.fnmatch(path, g) for g in meta.get("generated_paths", []))
LOCKFILES = re.compile(
    r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lockb?|poetry\.lock|Cargo\.lock|uv\.lock|Gemfile\.lock|go\.sum)$"
)
JUNK_PATH = re.compile(r"(^|/)(\.DS_Store|\.idea|\.vscode|\.env(?:\.[^/]*)?|coverage|dist|build|\.turbo|__pycache__|node_modules)(/|$)|\.(pem|key|pyc)$")
CONVENTIONAL_DIRS = {"node_modules", "dist", "build", "coverage", ".idea", ".vscode"}

SLASH_COMMENT_EXT = re.compile(r"\.(m?[jt]sx?|cjs|vue|svelte|go|rs|java|kt|swift|c|cc|cpp|h|hpp|cs|scss|less|css|dart|php)$")
HASH_COMMENT_EXT = re.compile(r"\.(py|sh|bash|zsh|ya?ml|toml|rb|r|pl|dockerfile)$|(^|/)Dockerfile$|(^|/)Makefile$")
HTML_COMMENT_EXT = re.compile(r"\.(vue|svelte|html?|xml)$")

DIRECTIVE = re.compile(r"(eslint-disable|@ts-(expect-error|ignore|nocheck)|prettier-ignore|biome-ignore|noqa|type:\s*ignore|istanbul ignore|c8 ignore|pragma)")

CODE_ATTRIBUTION = [
    (re.compile(r"generated (with|by|using) .{0,20}(claude|chatgpt|gpt|copilot|cursor|codex|gemini|\bai\b)", re.I), "generator footer"),
]

HARD_AI_TELLS = [
    (re.compile(r"co-authored-by:.*(claude|anthropic|openai|copilot|gpt|cursor|codex|gemini|noreply@anthropic)", re.I), "AI co-author trailer"),
    (re.compile(r"NEEDS USER DECISION|TODO\(user\)|<placeholder>|\[insert", re.I), "unresolved placeholder"),
]

SOFT_TELLS = [
    r"\bcomprehensive(ly)?\b", r"\brobust(ly|ness)?\b", r"\bseamless(ly)?\b", r"\bleverag(e|es|ed|ing)\b",
    r"\butiliz(e|es|ed|ing)\b", r"\bdelv(e|es|ing)\b", r"\bstreamlin(e|es|ed|ing)\b", r"\bcutting-edge\b",
    r"\belevat(e|es|ing)\b", r"\bempower(s|ing)?\b", r"\bmeticulous(ly)?\b", r"\bintricate\b", r"\bpivotal\b",
    r"\bcrucial\b", r"\bfurthermore\b", r"\bmoreover\b", r"\badditionally\b", r"\bin conclusion\b",
    r"\bthis (pr|pull request) (aims|introduces|adds)\b", r"\bgame[- ]changer\b", r"\bbest practices?\b",
    r"\benhanc(e|es|ed|ing|ement)\b", r"\bensur(e|es|ing)\b", r"\bkey (changes|improvements)\b",
    r"\bnow (properly|correctly)\b", r"\bgracefully\b", r"\bsignificantly\b",
    r"\b(claude|chatgpt)\b",
]
SOFT_TELLS = [re.compile(p, re.I) for p in SOFT_TELLS]
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def comment_text(path, line):
    s = line.strip()
    if not s or s == "*/" or s.startswith("#!"):
        return ""
    if HASH_COMMENT_EXT.search(path):
        try:
            return next((token.string for token in tokenize.generate_tokens(io.StringIO(line).readline) if token.type == tokenize.COMMENT), "")
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return ""
    if SLASH_COMMENT_EXT.search(path):
        if s == "*" or s.startswith("* "):
            return s
        comments, at = [], 0
        javascript = bool(re.search(r"\.(m?[jt]sx?|cjs|vue|svelte)$", path))
        while at < len(line):
            if line.startswith("//", at):
                comments.append(line[at:])
                break
            if line.startswith("/*", at):
                end = line.find("*/", at + 2)
                end = len(line) if end < 0 else end + 2
                comments.append(line[at:end])
                at = end
            elif line[at] in "\"'`":
                quote = line[at]
                at += 1
                while at < len(line) and line[at] != quote:
                    at += 2 if line[at] == "\\" else 1
                at += 1
            elif javascript and line[at] == "/" and re.search(r"(?:^|[=([{,:;!?&|]|\b(?:return|throw|case|yield))\s*$", line[:at]):
                at += 1
                in_class = False
                while at < len(line):
                    char = line[at]
                    at += 1
                    if char == "\\":
                        at += 1
                    elif char == "[":
                        in_class = True
                    elif char == "]":
                        in_class = False
                    elif char == "/" and not in_class:
                        break
            else:
                at += 1
        if comments:
            return " ".join(comments)
    if HTML_COMMENT_EXT.search(path):
        return " ".join(re.findall(r"<!--.*?(?:-->|$)", line))
    return ""


def is_comment(path, line):
    return bool(comment_text(path, line))


HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_diff(text):
    """Per file: added lines (text and new line number) and removed lines (old line number)."""
    files, current, old_path, old_no, new_no, hunk = {}, None, None, 0, 0, 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = old_path = None
        elif line.startswith("+++ "):
            name = line[4:]
            current = old_path if name == "/dev/null" else name[2:] if name.startswith("b/") else name
            if current:
                files.setdefault(current, {"deleted": name == "/dev/null", "added": [], "removed": 0, "added_at": [], "removed_at": [], "added_hunk": [], "removed_text": [], "removed_hunk": []})
        elif line.startswith("--- "):
            name = line[4:]
            old_path = name[2:] if name.startswith("a/") else name
        elif line.startswith("@@"):
            m = HUNK.match(line)
            if m:
                old_no, new_no = int(m.group(1)), int(m.group(2))
                hunk = hunk + 1 if current else 0
        elif current and line.startswith("+"):
            files[current]["added"].append(line[1:])
            files[current]["added_at"].append(new_no)
            files[current]["added_hunk"].append(hunk)
            new_no += 1
        elif current and line.startswith("-"):
            files[current]["removed"] += 1
            files[current]["removed_at"].append(old_no)
            files[current]["removed_text"].append(line[1:])
            files[current]["removed_hunk"].append(hunk)
            old_no += 1
    return files


def rust_test_module_start(clone, ref, path):
    """First line of the trailing test section in a Rust file at ref: a #[cfg(test)] module or, as in Oxc rules, a bare #[test] fn."""
    if not path.endswith(".rs"):
        return None
    proc = run(["git", "-C", str(clone), "show", f"{ref}:{path}"], check=False)
    if proc.returncode != 0:
        return None
    for number, line in enumerate(proc.stdout.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#[cfg(test)]") or stripped.startswith("#[test]"):
            return number
    return None


ASSERTION = re.compile(r"\b(expect|assert\w*|should|t\.(is|deepEqual|true|false|throws)|(?:not)?(?:[Dd]eep)?[Ss]trictEqual|toMatch\w*|toEqual|toBe\w*|toHave\w*|toThrow\w*|toContain\w*)\b|(?<![\w.])(?:not\.)?(?:is|equal|throws)\s*\(")
WEAK_ASSERTION = re.compile(r"\.(toBeDefined|toBeTruthy|toBeInTheDocument|not\.toBeNull|not\.toBeUndefined)\(\)|\bassert\s+\w+\s*$|assert\.ok\(")
TEST_BLOCK = re.compile(r"\b(it|test)(\.each\([^)]*\))?\(|\bdef test_\w+")
RUST_TEST = re.compile(r"#\[\s*test\s*\]")
GO_TEST = re.compile(r"\bfunc Test\w*\s*\(")


def go_assertions(text):
    aliases = {"require", "assert"}
    for match in re.finditer(r'(?:\b(\w+)\s+)?"github\.com/stretchr/testify/(require|assert)"', text):
        aliases.add(match.group(1) or match.group(2))
    names = "|".join(re.escape(name) for name in sorted(aliases))
    for match in re.finditer(rf"\b(\w+)\s*:=\s*(?:{names})\.New\s*\(", text):
        aliases.add(match.group(1))
    names = "|".join(re.escape(name) for name in sorted(aliases))
    methods = (
        "Equal|EqualValues|Exactly|NotEqual|NoError|Error|ErrorIs|ErrorAs|EqualError|"
        "Contains|NotContains|Len|Empty|NotEmpty|Greater|GreaterOrEqual|Less|LessOrEqual|"
        "True|False|Nil|NotNil|Panics|PanicsWithValue|PanicsWithError|NotPanics|"
        "WithinDuration|InDelta|InEpsilon|Eventually|Never|ElementsMatch|Subset|JSONEq|Regexp|"
        "Zero|NotZero|Same|NotSame|IsType|Implements|NotSubset|NotRegexp|Negative|Positive"
    )
    return re.compile(ASSERTION.pattern + rf"|\bt\.(?:Fatal|Error)f?\s*\(|\b(?:{names})\.(?:{methods})f?\s*\(")


def weak_tests(files):
    """New tests that cannot fail for the right reason: no assertion, or only existence checks."""
    out = []
    for path, d in files.items():
        if not TEST_PATH.search(path) or SNAPSHOT_PATH.search(path):
            continue
        added = "\n".join(d["added"])
        is_rust, is_go = path.endswith(".rs"), path.endswith(".go")
        pattern = RUST_TEST if is_rust else GO_TEST if is_go else TEST_BLOCK
        matches = list(pattern.finditer(added))
        if not matches:
            continue
        assertion = go_assertions(added) if is_go else ASSERTION
        blocks = [added]
        if is_rust or is_go:
            blocks = [added[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(added)] for i, m in enumerate(matches)]
        for block in blocks:
            if is_rust and re.search(r"#\[\s*should_panic\b", block):
                continue
            if not assertion.search(block):
                out.append(f"{path}: new test cases without any assertion")
                continue
            strong = [line for line in block.splitlines() if assertion.search(line) and not WEAK_ASSERTION.search(line)]
            if not strong:
                out.append(f"{path}: new tests only assert existence/truthiness; assert the behaviour the fix changes")
    return out


def sensitive_matches(text):
    return privacy.matches(text, markers=identity_markers())


def sensitive_findings(label, text, allow=(), *, prose=False, waive=()):
    return privacy.findings(label, text, allow, prose=prose, markers=identity_markers(), waive=waive)


def private_findings(label, text):
    return sensitive_findings(label, text)


def prose_findings(label, text, allowed_lines=(), allow=(), waive=()):
    fails, soft = sensitive_findings(label, text, allow, prose=True, waive=waive), []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line in allowed_lines:
            continue
        for rx, why in HARD_AI_TELLS + (CODE_ATTRIBUTION if label.startswith("commit ") else []):
            if rx.search(line):
                fails.append(f"{label}: {why}: {line[:120]}")
        for rx in SOFT_TELLS:
            m = rx.search(line)
            if m:
                soft.append(f"{label}: '{m.group(0)}' in: {line[:100]}")
    em_dashes = text.count("—")
    if em_dashes > 2:
        soft.append(f"{label}: {em_dashes} em dashes")
    emoji = EMOJI.findall(text)
    if emoji:
        soft.append(f"{label}: emoji {''.join(emoji[:5])}")
    return [privacy.redact(f) for f in fails], [privacy.redact(s) for s in soft]


def parse_pr_md(text):
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# "):
        return None, text
    return lines[0][2:].strip(), "\n".join(lines[1:]).strip() + "\n"


def follows_convention(path, tracked):
    """A build-style directory is fine where upstream already commits one in sibling directories,
    e.g. Knip's fixtures/plugins/<name>/node_modules. Secrets and editor droppings never are."""
    parts = path.split("/")
    if JUNK_PATH.search(parts[-1]):
        return False
    junk = next((i for i, part in enumerate(parts[:-1]) if JUNK_PATH.search(part + "/")), None)
    if junk is None or parts[junk] not in CONVENTIONAL_DIRS:
        return False
    prefix = parts[:max(junk - 1, 0)]
    for other in tracked:
        segments = other.split("/")
        if len(segments) > junk and segments[junk] == parts[junk] and segments[:len(prefix)] == prefix:
            return True
    return False


def check(item):
    meta, clone = item.meta, item.clone
    limits = CONFIG["limits"]
    fails, warns, soft = [], [], []
    base = item.merge_base()
    branch = meta["branch"]
    diff = git(clone, "diff", "--unified=0", "--no-color", "--no-ext-diff", f"{base}..{branch}")
    files = parse_diff(diff)
    stats = {"files": len(files), "added": 0, "removed": 0, "src_comments": 0, "test_comments": 0}

    if not files:
        fails.append("branch has no changes against its merge base")

    touched_test = False
    tracked = None
    for path, d in files.items():
        is_test = bool(TEST_PATH.search(path))
        is_doc = bool(DOC_PATH.search(path))
        generated = is_generated(path, meta)
        touched_test |= is_test
        if LOCKFILES.search(path) and not meta.get("allow_lockfile"):
            fails.append(f"lockfile changed: {path} (set allow_lockfile with a reason if a dependency change is the point of the PR)")
        if not d["deleted"] and JUNK_PATH.search(path):
            if tracked is None:
                tracked = git(clone, "ls-tree", "-r", "--name-only", base).splitlines()
            if follows_convention(path, tracked):
                warns.append(f"{path} sits in a directory upstream already commits (repo convention)")
            else:
                fails.append(f"editor/build/secret artifact in diff: {path}")
        if generated:
            stats["generated_lines"] = stats.get("generated_lines", 0) + len(d["added"]) + d["removed"]
        elif not (LOCKFILES.search(path) or SNAPSHOT_PATH.search(path)):
            stats["added"] += len(d["added"])
            stats["removed"] += d["removed"]
            inline = 0
            if not is_test and path.endswith(".rs"):
                head_start = rust_test_module_start(clone, branch, path)
                base_start = rust_test_module_start(clone, base, path)
                inline += sum(1 for n in d["added_at"] if head_start and n >= head_start)
                inline += sum(1 for n in d["removed_at"] if base_start and n >= base_start)
                d["inline_test_from"] = head_start
            key = "test_lines" if is_test else "doc_lines" if is_doc else "src_lines"
            stats[key] = stats.get(key, 0) + len(d["added"]) + d["removed"] - inline
            if inline:
                touched_test = True
                stats["test_lines"] = stats.get("test_lines", 0) + inline
                stats["inline_rust_test_lines"] = stats.get("inline_rust_test_lines", 0) + inline
        if is_doc or generated:
            continue
        inline_from = d.get("inline_test_from")
        replaced = {}
        for h, text in zip(d["removed_hunk"], d["removed_text"]):
            if is_comment(path, text):
                replaced[h] = replaced.get(h, 0) + 1
        for at, h, line in zip(d["added_at"], d["added_hunk"], d["added"]):
            s = line.strip()
            in_inline_tests = bool(inline_from and at >= inline_from)
            if re.search(r"\b(TODO|FIXME|XXX|HACK)\b", s):
                fails.append(f"{path}: new TODO/FIXME: {s[:100]}")
            comment = comment_text(path, line)
            if comment:
                if DIRECTIVE.search(comment):
                    warns.append(f"{path}: lint/type directive added: {s[:100]}")
                elif is_test or in_inline_tests:
                    stats["test_comments"] += 1
                elif replaced.get(h, 0) > 0:
                    replaced[h] -= 1
                    warns.append(f"{path}: edited an existing comment: {s[:100]}")
                else:
                    stats["src_comments"] += 1
                    if not meta.get("comment_justification"):
                        fails.append(f"{path}: new comment in source: {s[:100]}")
                for rx, why in HARD_AI_TELLS + CODE_ATTRIBUTION:
                    if rx.search(comment):
                        fails.append(f"{path}: {why} in comment: {comment[:100]}")
            if not (is_test or in_inline_tests) and re.search(r"\bconsole\.(log|debug)\(|^\s*debugger;?\s*$", line):
                fails.append(f"{path}: debug statement: {s[:100]}")
            if is_test and re.search(r"\b(it|test|describe)(?:\s*\.\s*\w+)*?\s*\.\s*only\s*(?:\(|\.)|\b(fit|fdescribe)\s*(?:\(|\.)", line):
                fails.append(f"{path}: focused test left in: {s[:100]}")

    weak = weak_tests(files)
    stats["weak_test_files"] = len(weak)
    fails.extend(weak)

    if stats["test_comments"] > 3:
        warns.append(f"{stats['test_comments']} new comment lines in tests; keep only what the test name cannot say")

    size = stats["added"] + stats["removed"]
    src = stats.get("src_lines", 0)
    tests = stats.get("test_lines", 0)
    hard_cap = limits["max_diff_lines_large"] if meta.get("kind") == "large" else limits["max_diff_lines"]
    if src > hard_cap:
        fails.append(f"{src} changed source lines (cap {hard_cap}); split it, or mark kind=large with maintainer buy-in")
    elif src > limits["warn_diff_lines"]:
        warns.append(f"{src} changed source lines; make sure every line serves the issue")
    if tests > limits.get("warn_test_lines", 1200):
        warns.append(f"{tests} changed test lines; keep fixtures shared and tests focused on the behaviour")

    if not touched_test and not meta.get("no_test_reason"):
        fails.append("no test file changed; add a regression test or set no_test_reason")

    ws = git(clone, "diff", "--check", f"{base}..{branch}", check=False)
    if ws:
        fails.append("whitespace errors: " + ws.splitlines()[0][:120])

    commits = git(clone, "log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x1e", f"{base}..{branch}").split("\x1e")
    commits = [c.strip("\n") for c in commits if c.strip()]
    if len(commits) > 3:
        warns.append(f"{len(commits)} commits on branch; squash noise commits unless the repo wants them")
    public = published.on_pr_branch(item, base)
    for c in commits:
        sha, name, email, cname, cemail, body = c.split("\x00", 5)
        accepted = {CONFIG["git_email"], *CONFIG.get("git_email_previous", [])}
        if email not in accepted and sha not in public:
            fails.append(f"commit {sha[:9]} author does not match the configured public identity")
        if cemail not in accepted and sha not in public:
            fails.append(f"commit {sha[:9]} committed as an unexpected public identity")
        fails += privacy.findings(f"commit {sha[:9]} identity", f"{name} {email} {cname} {cemail}", tuple(meta.get("sensitive_allow", [])),
                                  markers=identity_markers(), waive=published.identity_waiver([1] if sha in public else []))
        f, s = prose_findings(f"commit {sha[:9]}", body, (), tuple(meta.get("sensitive_allow", [])),
                              waive=published.identity_waiver(published.trailer_lines(body) if sha in public else []))
        fails += f
        soft += s

    pr_md = item.file("pr.md")
    if pr_md.exists():
        title, body = parse_pr_md(pr_md.read_text())
        if title is None:
            fails.append("pr.md must start with '# <PR title>' followed by the exact PR body")
        else:
            if len(title) > 80:
                warns.append(f"PR title is {len(title)} chars")
            pattern = meta.get("title_pattern")
            if pattern and not re.search(pattern, title):
                fails.append(f"PR title does not match repo convention {pattern!r}: {title}")
            f, s = prose_findings("pr.md", title + "\n" + body, (), tuple(meta.get("sensitive_allow", [])))
            fails += f
            soft += s
            body_lines = [l for l in body.splitlines() if l.strip() and not l.strip().startswith("<!--")]
            if len(body_lines) > limits["max_pr_body_lines"]:
                warns.append(f"PR body has {len(body_lines)} non-empty lines; maintainers skim, cut it down")
            if meta.get("issue") and meta.get("target", "issue") == "issue" and str(meta["issue"]) not in body and meta.get("line") == "oss":
                warns.append(f"PR body does not reference issue #{meta['issue']}")
    else:
        fails.append("pr.md missing")

    if len(soft) > limits["max_soft_slop_hits"]:
        fails.append(f"{len(soft)} filler/AI-style phrases (cap {limits['max_soft_slop_hits']}); rewrite plainly")
    elif soft:
        warns.extend(soft)
    from factory.core import leaks, privacy_git

    try:
        head = git(clone, "rev-parse", branch)
        # Exclude only public history: a stacked item's lower branch is local, and the scan treats
        # content a merge keeps from an excluded parent as already public.
        public_base = git(clone, "merge-base", f"origin/{meta.get('base', 'main')}", head)
        fails += privacy_git.scan(clone, [head, "^" + public_base], names=[branch],
                                  allow=tuple(meta.get("sensitive_allow", [])),
                                  binary_reviewed=meta.get("binary_reviewed"), item_dir=item.dir, on_pr_branch=public)
    except (ValueError, OSError, TimeoutError, RuntimeError):
        fails.append("outgoing privacy scan incomplete; publishing blocked")
    if pr_md.exists():
        fails += leaks.scan(text=pr_md.read_text(), item_dir=item.dir, label="pr.md")
    fails = list(dict.fromkeys(privacy.redact(f) for f in fails))
    warns = [privacy.redact(w) for w in warns]
    soft = [privacy.redact(s) for s in soft]

    return {"ok": not fails, "fails": fails, "warns": warns, "soft": soft, "stats": stats, "diff_lines": size}
