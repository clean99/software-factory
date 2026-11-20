"""Registered gate checks and an index of the checks enforced by existing commands."""
import fnmatch
import pathlib
import re
import shlex

from factory.core import receipts, verdicts
from factory.util import CONFIG, ROOT, CommandError, git, read_json, run

MODES = ("prepare", "review", "release")
_REGISTRY = {}
LEGACY = {
    "gate-artifacts": "factory.core.gate.core_checks: required item artifacts",
    "self-review": "factory.core.gate.core_checks: self-review verdict",
    "user-impact": "factory.core.gate.user_impact_missing: user-impact evidence lines",
    "independent-review": "factory.core.gate.review_independence: reviewer independence",
    "review-head": "factory.core.gate.core_checks: review bound to the current head",
    "redgreen": "factory.core.redgreen.run and factory.core.gate.core_checks: red-to-green proof",
    "hygiene": "factory.core.slop.check: diff and prose hygiene",
    "merges-cleanly": "factory.core.gate.core_checks: merge-tree conflict check",
    "issue-free": "factory.core.gate.oss_checks: fresh qualification",
    "outsiders-accepted": "factory.core.gate.oss_checks: outside contributors accepted",
    "ai-policy": "factory.core.gate.oss_checks: contribution policy permits the work",
    "ai-disclosure": "factory.core.gate.oss_checks: factory-owned PR disclosure and upstream format",
    "issue-precondition": "factory.core.gate.precondition_checks: live issue or precedent evidence",
    "issue-first": "factory.core.gate.oss_checks: issue-first decision",
    "owner-prose": "factory.core.gate.oss_checks: owner-authored prose policy",
    "dco": "factory.core.gate.oss_checks: required commit sign-offs",
    "coordinator-review": "factory.core.gate.coordinator_check: coordinator review at the current head",
    "approval": "factory.core.gate.run: approval bound to head and PR text",
    "hooks-drift": "factory.util.global_hooks_drift: global hook file baseline",
    "qualify": "factory.oss.qualify.run: issue ownership and competing work",
    "policy-scan": "factory.oss.policy.run: repository contribution rules",
    "flood-caps": "factory.oss.submit.flood_check: contribution volume caps",
    "stale-open": "factory.oss.submit.flood_check: stale unmerged work pauses new submissions",
    "missing-lesson": "factory.core.ledger.missing_lessons and factory.oss.submit.preconditions: lessons owed",
    "privacy-scan": "factory.core.privacy.findings and factory.core.privacy_git.scan: outgoing privacy checks",
    "gitleaks": "factory.core.leaks.scan: outgoing secret scan",
    "commit-wrapper": "factory.core.commit.run_commit: checked commit workflow",
    "followup": "factory.oss.followup.run: open contribution follow-up",
    "stop-check": "factory.core.stopcheck.check: unfinished work on session exit",
    "thread-reply": "factory.oss.thread_reply.run: approved replies in the correct live thread",
    "clone-heal": "factory.core.clone_heal.heal: safe clone recovery",
    "ship-preconditions": "factory.oss.ship.Ship.preconditions and factory.oss.submit.preconditions: pre-publication refusals",
    "health": "factory.oss.health.run: repository contributor health",
    "watch": "factory.oss.watch.run: bounded opportunity scan",
}


def check(id, title, lessons=(), modes=MODES, schema_min=2):
    def wrap(fn):
        _REGISTRY[id] = {"id": id, "title": title, "lessons": tuple(lessons), "modes": tuple(modes), "schema_min": schema_min, "fn": fn}
        return fn
    return wrap


def registry():
    return dict(_REGISTRY)


def run_extra(item, rep, mode):
    from factory.core import review

    schema = verdicts.schema(item)
    for entry in _REGISTRY.values():
        if mode in entry["modes"] and schema >= entry["schema_min"]:
            entry["fn"](item, rep)


def noop_reason(argv):
    if not argv or not argv[0].strip():
        return "an empty command cannot test anything"
    name = pathlib.Path(argv[0]).name
    if len(argv) > 1 and all(arg in ("--version", "--help") for arg in argv[1:]):
        return "version or help flags alone cannot test code"
    known_tool = (
        name in ("node", "npm", "pnpm", "yarn", "bun", "deno", "git", "go", "cargo", "rustc", "ruby", "java", "tsc")
        or re.fullmatch(r"(python|pypy)([0-9]+(\.[0-9]+)*[a-z]*)?", name) is not None
    )
    help_args = ("-V", "-v", "-h", "-?", "-version", "-help", "--version", "--help", "version", "help")
    if known_tool and len(argv) > 1 and all(arg in help_args for arg in argv[1:]):
        return "version or help flags alone cannot test code"
    if name in ("true", ":", "echo"):
        return f"{name} only reports success or prints text; it cannot test code"
    if name == "exit" and argv[1:] in ([], ["0"]):
        return "exit 0 cannot test anything"
    if name in ("test", "["):
        args = argv[1:-1] if name == "[" and argv[-1:] == ["]"] else argv[1:]
        file_tests = {"-b", "-c", "-d", "-e", "-f", "-g", "-h", "-k", "-p", "-r", "-s", "-t",
                      "-u", "-w", "-x", "-G", "-L", "-N", "-O", "-S", "-ef", "-nt", "-ot"}
        if not any(arg in file_tests or "$" in arg or "`" in arg for arg in args):
            return "test or [ with only constants cannot test code"
    if name in ("sh", "bash", "zsh") and len(argv) >= 3 and argv[1] in ("-c", "-lc"):
        try:
            script = re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"|\\.|#[^\n]*",
                            lambda match: "" if match[0].startswith("#") else match[0], argv[2])
            lexer = shlex.shlex(script, posix=True, punctuation_chars=";&|\n")
            lexer.commenters = ""
            lexer.whitespace = " \t\r"
            lexer.whitespace_split = True
            commands, command = [], []
            for token in lexer:
                if token and all(char in ";&|\n" for char in token):
                    if command:
                        commands.append(command)
                        command = []
                else:
                    command.append(token)
            if command:
                commands.append(command)
        except ValueError:
            return None
        if not commands or all(noop_reason(command) for command in commands):
            return "an empty or constant-only shell script cannot test code"
    return None


def matches_required(item, receipt, entry):
    if (not isinstance(entry, dict) or not isinstance(entry.get("argv"), list) or not entry["argv"]
            or any(not isinstance(arg, str) for arg in entry["argv"])
            or (entry.get("cwd") is not None and not isinstance(entry["cwd"], str))):
        return False
    argv = [receipts.scrub_home(arg) for arg in entry["argv"]]
    cwd = receipts.scrub_home(str(receipts.command_cwd(item, entry.get("cwd"))))
    return receipt.get("argv") == argv and receipt.get("cwd") == cwd


@check("verify-receipts", "Required checks have passing receipts", lessons=("L-043", "L-051"), modes=("review", "release"))
def verify_receipts(item, rep):
    required = item.meta.get("required_checks")
    fix = "include the redgreen.json command and cwd in item.json required_checks unless no_test_reason exempts red-to-green; list the repository's CI commands, then commit or revert stray tracked changes and run each with factory run --item DIR --label LABEL -- CMD"
    if not isinstance(required, list) or not required:
        rep.add("verify-receipts", False, "required_checks must be a non-empty list", fix)
        return
    problems = []
    if not item.meta.get("no_test_reason"):
        try:
            command, cwd = receipts.redgreen_command(item)
            present = any(isinstance(entry, dict) and isinstance(entry.get("argv"), list)
                          and all(isinstance(arg, str) for arg in entry["argv"])
                          and (entry.get("cwd") is None or isinstance(entry["cwd"], str))
                          and receipts.command_matches([receipts.scrub_home(arg) for arg in entry["argv"]], command)
                          and receipts.command_cwd(item, entry.get("cwd")) == cwd for entry in required)
            if not present:
                problems.append("required_checks must include the exact regression command and cwd from redgreen.json; add that entry and capture its receipt")
        except ValueError as error:
            problems.append(str(error))
    head = item.head_sha()
    available = [receipt for receipt in receipts.load(item) if receipts.valid(receipt)
                 and type(receipt.get("exit")) is int and receipt["exit"] == 0]
    for index, entry in enumerate(required):
        if (not isinstance(entry, dict) or not isinstance(entry.get("label"), str) or not entry["label"].strip()
                or not isinstance(entry.get("argv"), list) or not entry["argv"]
                or any(not isinstance(arg, str) for arg in entry["argv"])
                or (entry.get("cwd") is not None and not isinstance(entry["cwd"], str))):
            problems.append(f"required_checks[{index}] needs label, argv (a non-empty list), and cwd (a path)")
            continue
        reason = noop_reason(entry["argv"])
        if reason:
            problems.append(f"{entry['label']}: {reason}; replace it with the actual CI check")
            continue
        found = any(matches_required(item, receipt, entry) and receipts.clean(receipt)
                    and isinstance(receipt.get("cwd_head"), str)
                    and verdicts.bound(item, head, receipt["cwd_head"]) for receipt in available)
        if not found:
            problems.append(f"{entry['label']}: no valid clean exit-0 receipt for this argv, cwd and head; commit or revert stray tracked changes and run again")
    rep.add("verify-receipts", not problems, "; ".join(problems) or "all required commands passed at this head", fix)


@check("changeset-required", "Repository-required companion files", lessons=("L-045",), schema_min=1)
def changeset_required(item, rep):
    repo = item.meta.get("repo", "")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        rep.add("changeset-required", False, "invalid repository name", "set item.json repo to owner/repo")
        return
    path = ROOT / "lines" / "oss" / "repos" / (repo.replace("/", "__") + ".rules.json")
    if not path.exists():
        return
    fix = "add the required companion file to this change; follow the repository's review checklist"
    try:
        data = read_json(path)
        rules = data.get("requires") if isinstance(data, dict) else None
        if not isinstance(rules, list) or any(not isinstance(rule, dict) or
                any(not isinstance(rule.get(key), str) or not rule[key] for key in ("changed", "needs", "why")) for rule in rules):
            raise ValueError("requires must be a list of changed/needs/why rules")
        head = item.head_sha()
        fields = git(item.clone, "diff", "--name-status", "--find-renames", "-z", f"{item.merge_base()}..{head}").rstrip("\0").split("\0")
        changed, index = {}, 0
        while index < len(fields) and fields[index]:
            status = fields[index]
            count = 2 if status.startswith(("R", "C")) else 1
            if index + count >= len(fields):
                raise ValueError("cannot read diff statuses; rerun the companion-file check")
            for name in fields[index + 1:index + 1 + count]:
                changed[name] = status
            index += count + 1
        companions = [name for name, status in changed.items() if status in ("A", "M")
                      and any(fnmatch.fnmatchcase(name, rule["needs"]) for rule in rules)
                      and run(["git", "-C", str(item.clone), "cat-file", "-e", f"{head}:{name}"], check=False).returncode == 0]
    except (OSError, ValueError, CommandError) as error:
        rep.add("changeset-required", False, str(error), f"repair or re-read {path.name} and the item diff")
        return
    missing = [f"{rule['changed']} needs {rule['needs']}: {rule['why']}" for rule in rules
               if any(fnmatch.fnmatchcase(name, rule["changed"]) for name in changed)
               and not any(fnmatch.fnmatchcase(name, rule["needs"]) for name in companions)]
    rep.add("changeset-required", not missing, "; ".join(missing) or "required companion files are present or not needed", fix)


@check("hook-env", "Global hooks have a recorded baseline", lessons=("L-035",), modes=("release",), schema_min=2)
def hook_env(item, rep):
    proc = run(["git", "config", "--global", "--get", "core.hooksPath"], check=False)
    if proc.returncode not in (0, 1):
        rep.add("hook-env", False, "could not read global core.hooksPath", "resolve the git configuration read error before releasing")
        return
    configured = bool(proc.stdout.strip())
    baseline = CONFIG.get("global_hooks_baseline")
    ok = not configured or isinstance(baseline, dict) and bool(baseline)
    if not configured:
        detail = "no global core.hooksPath"
    elif ok:
        detail = "global hooks have a baseline"
    else:
        detail = "global core.hooksPath is set but global_hooks_baseline is missing"
    rep.add("hook-env", ok, detail,
            "ask the owner to verify the global hooks and record their baseline; do not change or disable the hooks")


def cmd_checks(args):
    from factory.core import lessons, review

    linked = {}
    for lesson in lessons.load():
        for token in lesson.get("enforced-by", "").partition(" — ")[0].split(","):
            token = token.strip()
            if token.startswith("check:"):
                linked.setdefault(token[6:], set()).add(lesson["id"])
    print("kind\tid\tlessons\tdescription")
    for id, description in LEGACY.items():
        print(f"legacy\t{id}\t{', '.join(sorted(linked.get(id, ()))) or '-'}\t{description}")
    for id, entry in registry().items():
        ids = set(entry["lessons"]) | linked.get(id, set())
        print(f"registered\t{id}\t{', '.join(sorted(ids)) or '-'}\t{entry['title']}")


def register(sub):
    sub.add_parser("checks", help="list legacy and registered checks with their lessons").set_defaults(fn=cmd_checks)
