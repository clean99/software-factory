import os
import pathlib
import re
import subprocess
from urllib.parse import urlsplit


WRITES = {
    "pr": {"create", "edit", "comment", "review"},
    "issue": {"create", "comment", "edit"},
    "release": {"create", "edit", "upload"},
    "gist": {"create"},
    "repo": {"create", "edit"},
}
READS = {
    "pr": {"list", "view", "diff", "checks", "status"},
    "issue": {"list", "view", "status"},
    "release": {"list", "view", "download", "verify", "verify-asset"},
    "gist": {"list", "view", "clone"},
    "repo": {"list", "view", "clone"},
    "auth": {"status", "token"},
    "run": {"list", "view", "watch", "download"},
    "workflow": {"list", "view"},
    "search": {"code", "commits", "issues", "prs", "repos"},
    "label": {"list"}, "ruleset": {"list", "view", "check"},
    "variable": {"list", "get"}, "secret": {"list"},
    "project": {"list", "view", "field-list", "item-list"},
    "cache": {"list"}, "extension": {"list", "search", "browse"},
    "config": {"get", "list"}, "alias": {"list"},
    "ssh-key": {"list"}, "gpg-key": {"list"},
}
FILE_FLAGS = {"--body-file", "--notes-file", "--input"}
VALUE_FLAGS = FILE_FLAGS | {
    "-F", "-f", "--field", "--raw-field", "-X", "--method", "-b", "--body",
    "-t", "--title", "--notes", "-n", "-R", "--repo", "--hostname", "-H", "--header",
    "-h", "--head", "-B", "--base", "--name", "--target", "-r", "--reviewer", "-a", "--assignee",
    "-l", "--label", "-m", "--milestone", "-p", "--project", "--category", "--discussion-category",
    "-s", "--source", "--remote", "-d", "--description", "--homepage", "-q", "--jq", "-T", "--template",
    "--attach", "--add-assignee", "--add-label", "--add-project", "--add-reviewer",
    "--remove-assignee", "--remove-label", "--remove-project", "--remove-reviewer",
    "--notes-start-tag", "--license", "--gitignore", "--team", "--default-branch",
    "--tag", "--filename", "--visibility", "--add-topic", "--remove-topic", "--desc",
}
SHORT_VALUES = {f for f in VALUE_FLAGS if len(f) == 2}
READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def options(args):
    opts, words = [], []
    i = 0
    while i < len(args):
        arg = args[i]
        key, equal, value = arg.partition("=")
        if arg == "--":
            words.extend(args[i + 1:])
            break
        if arg.startswith("--") and equal:
            opts.append((key, value))
        elif arg == "-h" and words[:2] not in (["repo", "create"], ["repo", "edit"]):
            opts.append(("--help", None))
        elif words[:1] == ["browse"] and arg in {"-n", "-s", "-p", "-r", "-a"}:
            opts.append((arg, None))
        elif ((words[:2] == ["pr", "review"] and arg in {"-a", "-r"})
              or (words[:2] in (["pr", "create"], ["pr", "list"], ["release", "create"]) and arg == "-d")
              or (words[:2] in (["release", "create"], ["gist", "create"]) and arg == "-p")):
            opts.append((arg, None))
        elif arg in VALUE_FLAGS:
            i += 1
            if i == len(args):
                raise ValueError("missing option value")
            opts.append((arg, args[i]))
        elif len(arg) > 2 and arg[:2] in SHORT_VALUES:
            opts.append((arg[:2], arg[2:]))
        elif arg.startswith("-") and arg != "-":
            opts.append((arg, None))
        else:
            words.append(arg)
        i += 1
    return opts, words


def graphql_operations(query):
    # Operation types occur only at the start of top-level definitions, not in fields or names.
    source = re.sub(r'""".*?"""|"(?:\\.|[^"\\])*"|#[^\n]*', ' ', query, flags=re.S)
    tokens = re.findall(r'[A-Za-z_][A-Za-z_0-9]*|[^\s,]', source)
    operations, depth, parens, definition = [], 0, 0, False
    for token in tokens:
        if not definition:
            if token not in {"query", "mutation", "subscription", "fragment", "{"}:
                return []
            if token != "fragment":
                operations.append("query" if token == "{" else token)
            definition = True
        if token == "(":
            parens += 1
        elif token == ")":
            parens -= 1
        elif not parens and token == "{":
            depth += 1
        elif not parens and token == "}":
            depth -= 1
            if depth == 0:
                definition = False
        if depth < 0 or parens < 0:
            return []
    return operations if not definition and not depth and not parens else []


def graphql_read(query):
    operations = graphql_operations(query)
    return bool(operations) and all(op == "query" for op in operations)


def gh_kind(args):
    if args[:1] in (["help"], ["--version"]):
        return "read"
    opts, words = options(args)
    help_values = [v for k, v in opts if k == "--help"]
    if help_values and help_values[-1] in {None, "true", "1", "t", "TRUE", "True", "T"}:
        return "read"
    if not words:
        return "read" if not args else "unsupported"
    if words[0] == "api":
        methods = [v.upper() for k, v in opts if k in {"-X", "--method"}]
        if methods and methods[-1] in READ_METHODS:
            return "read"
        fields = [(k, v) for k, v in opts if k in {"-f", "-F", "--field", "--raw-field"}]
        if words[1:2] == ["graphql"] and (not methods or methods[-1] == "POST"):
            queries = [v.partition("=")[2] for k, v in fields if v.startswith("query=")]
            ambiguous = any(k == "--input" for k, _ in opts) or any(v.startswith("query[") for _, v in fields)
            return "read" if len(queries) == 1 and graphql_read(queries[0]) and not ambiguous else "write"
        return "write" if methods or fields or any(k == "--input" for k, _ in opts) else "read"
    if words[0] in {"help", "version", "status", "completion", "browse"}:
        return "read"
    if len(words) == 1:
        return "read" if words[0] in READS else "unsupported"
    if words[1] in WRITES.get(words[0], set()):
        return "write"
    return "read" if words[1] in READS.get(words[0], set()) else "unsupported"


def github(url):
    if re.match(r"^(?:[^/@:]+@)?(?:ssh\.)?github\.com:", url, re.I):
        return True
    try:
        return (urlsplit(url).hostname or "").lower() in {"github.com", "ssh.github.com"}
    except ValueError:
        return False


def github_repo(url):
    m = re.match(r"^(?:[^/@:]+@)?(?:ssh\.)?github\.com:/?(?P<path>.+)$", url, re.I)
    path = m.group("path") if m else (urlsplit(url).path if github(url) else "")
    parts = [p for p in path.strip("/").removesuffix(".git").split("/") if p]
    return "/".join(parts[-2:]).lower() if len(parts) >= 2 else ""


def private_destination(url):
    """Pushes to the owner's own private repos (private_push_repos in .factory.local.json) are not publication."""
    from factory.util import CONFIG

    return github(url) and github_repo(url) in {r.lower() for r in CONFIG.get("private_push_repos", [])}


def dynamic(value):
    return bool(re.search(r"[$`]|[<>]\(", value))


def git_parts(args, cwd):
    prefix = []
    i = 0
    while i < len(args) and args[i].startswith("-"):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        if arg in {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}:
            if i + 1 == len(args):
                raise ValueError("missing git option value")
            value = args[i + 1]
            if arg == "-C":
                cwd = cwd / value if cwd is not None and not dynamic(value) else None
            else:
                prefix.extend((arg, value))
            i += 2
        elif arg.startswith("-C"):
            cwd = cwd / arg[2:] if cwd is not None and not dynamic(arg) else None
            i += 1
        elif arg.startswith(("--git-dir=", "--work-tree=", "--namespace=", "--config-env=", "-c")):
            prefix.append(arg)
            i += 1
        elif arg in {"--no-pager", "--paginate", "-P", "-p", "--bare", "--no-optional-locks", "--no-replace-objects", "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs", "--no-lazy-fetch", "--no-advice"}:
            prefix.append(arg)
            i += 1
        elif arg in {"--help", "-h", "--version", "-v"}:
            return cwd, [], ["help"]
        else:
            i += 1
    return cwd, prefix, args[i:]


def local_git(cwd, prefix, *args, env=None):
    if cwd is None:
        raise ValueError("unresolved git directory")
    result = subprocess.run(["git", *prefix, *args], cwd=cwd, env=env, text=True,
                            capture_output=True, timeout=2)
    return result.stdout.strip() if result.returncode == 0 else ""


def push_target(args):
    target = None
    words = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            words.extend(args[i + 1:])
            break
        if arg in {"--repo", "--receive-pack", "--exec", "-o", "--push-option"}:
            i += 1
            if i == len(args):
                raise ValueError("missing push option value")
            if arg == "--repo":
                target = args[i]
        elif arg.startswith("--repo="):
            target = arg.split("=", 1)[1]
        elif not arg.startswith("-"):
            words.append(arg)
        i += 1
    return target or (words[0] if words else None)


def push_urls(args, cwd, prefix=(), env=None):
    target = push_target(args)
    read = lambda *a: local_git(cwd, prefix, *a, env=env)
    if target is None:
        branch = read("symbolic-ref", "--short", "HEAD")
        target = (read("config", "--get", f"branch.{branch}.pushRemote")
                  or read("config", "--get", "remote.pushDefault")
                  or read("config", "--get", f"branch.{branch}.remote") or "origin")
    if dynamic(target):
        raise ValueError("unresolved push remote")
    # Literal URLs participate in pushInsteadOf and insteadOf rewriting too.
    direct = bool(re.match(r"(?:[\w+.-]+://|[^/\s:]+:|[./~])", target))
    urls = [] if direct else read("remote", "get-url", "--push", "--all", target).splitlines()
    if not urls:
        # remote get-url omits remotes defined only by command-local config.
        pushurls = [] if direct else read("config", "--get-all", f"remote.{target}.pushurl").splitlines()
        urls = pushurls or ([] if direct else read("config", "--get-all", f"remote.{target}.url").splitlines())
        rewrites = read("config", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$").splitlines()

        def rewrite(url):
            for suffix in ((".insteadof",) if pushurls else (".pushinsteadof", ".insteadof")):
                matches = []
                for line in rewrites:
                    key, _, value = line.partition(" ")
                    if key.endswith(suffix) and value and url.startswith(value):
                        matches.append((len(value), key[4:-len(suffix)] + url[len(value):]))
                if matches:
                    return max(matches, key=lambda row: row[0])[1]
            return url

        urls = [rewrite(url) for url in (urls or [target])]
        if not direct and urls == [target]:
            raise ValueError("unresolved push remote")
    for url in urls:
        if "://" in url:
            host = urlsplit(url).hostname or ""
        elif re.match(r"^[^/\s:]+:", url):
            host = url.split(":", 1)[0].rsplit("@", 1)[-1]
        else:
            host = ""
        if host and "." not in host and ":" not in host and host != "localhost":
            raise ValueError("unresolved SSH or transport alias")
    return urls
