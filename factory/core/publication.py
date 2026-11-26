"""Preflight explicit publications with privacy checks and contribution release approval."""
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
from urllib.parse import urlsplit

from factory.core import images, leaks, privacy, privacy_git
from factory.core.publish_commands import FILE_FLAGS, gh_kind, github, options, private_destination, push_target, push_urls
from factory.util import CONFIG, ROOT, CommandError, Item, gh_json, read_json, GH_READ_TIMEOUT


BODY = {"--body", "-b", "--body-file", "-F"}
INTERACTIVE = {"--web", "--editor", "--fill", "--fill-first", "--fill-verbose", "--generate-notes", "--notes-from-tag", "--push", "--template", "-T", "--recover"}
COMMON = {"-R", "--repo", "--hostname"}
FLAGS = {
    ("pr", "create"): BODY | {"--title", "-t", "--head", "-H", "-h", "--base", "-B", "--draft", "-d", "--assignee", "-a", "--reviewer", "-r", "--label", "-l", "--milestone", "-m", "--project", "-p", "--no-maintainer-edit"},
    ("pr", "edit"): BODY | {"--title", "-t", "--base", "-B", "--milestone", "-m", "--remove-milestone", "--attach", "--add-assignee", "--add-label", "--add-project", "--add-reviewer", "--remove-assignee", "--remove-label", "--remove-project", "--remove-reviewer"},
    ("pr", "comment"): BODY | {"--edit-last", "--create-if-none", "--attach"},
    ("pr", "review"): BODY | {"--approve", "-a", "--comment", "-c", "--request-changes", "-r"},
    ("issue", "create"): BODY | {"--title", "-t", "--assignee", "-a", "--label", "-l", "--milestone", "-m", "--project", "-p"},
    ("issue", "edit"): BODY | {"--title", "-t", "--milestone", "-m", "--remove-milestone", "--attach", "--add-assignee", "--add-label", "--add-project", "--remove-assignee", "--remove-label", "--remove-project"},
    ("issue", "comment"): BODY | {"--edit-last", "--create-if-none", "--attach"},
    ("release", "create"): {"--notes", "-n", "--notes-file", "-F", "--title", "-t", "--target", "--discussion-category", "--draft", "-d", "--prerelease", "-p", "--latest", "--verify-tag", "--fail-on-no-commits"},
    ("release", "edit"): {"--notes", "-n", "--notes-file", "-F", "--title", "-t", "--target", "--draft", "--prerelease", "--latest", "--tag", "--verify-tag"},
    ("release", "upload"): {"--clobber"},
    ("gist", "create"): {"--desc", "-d", "--filename", "-f", "--public", "-p"},
    ("repo", "create"): {"--description", "-d", "--homepage", "-h", "--public", "--private", "--internal", "--source", "-s", "--remote", "-r", "--clone", "-c", "--team", "-t"},
    ("repo", "edit"): {"--description", "-d", "--homepage", "-h", "--default-branch", "--add-topic", "--remove-topic", "--visibility", "--accept-visibility-change-consequences", "--enable-issues", "--enable-wiki", "--enable-discussions", "--enable-projects", "--enable-squash-merge", "--enable-merge-commit", "--enable-rebase-merge", "--enable-auto-merge", "--delete-branch-on-merge", "--allow-forking", "--allow-update-branch", "--template"},
}
API_FLAGS = {"-X", "--method", "-f", "--raw-field", "-F", "--field", "--input", "-H", "--header", "--hostname", "--silent", "--include", "-i", "--jq", "-q"}


def context(item_dir=None, files=()):
    if item_dir is not None:
        folder = pathlib.Path(item_dir).resolve()
        meta = json.loads((folder / "item.json").read_text())
    else:
        folder, meta = None, {}
        for file in files:
            parent = file.parent.resolve()
            if parent.parent.parent == (ROOT / "runs").resolve() and (parent / "item.json").is_file():
                if folder is not None and folder != parent:
                    raise ValueError("publication references multiple items; select --item")
                folder, meta = parent, json.loads((parent / "item.json").read_text())
    allow = meta.get("sensitive_allow", [])
    if not isinstance(allow, list) or any(not isinstance(v, str) for v in allow):
        raise ValueError("item sensitive_allow must be exact strings")
    return folder, meta, {"allow": tuple(allow), "binary_reviewed": meta.get("binary_reviewed") is True, "item_dir": folder}


def text_findings(label, text, ctx):
    return privacy.findings(label, text, ctx["allow"], prose=True) + leaks.scan(text=text, item_dir=ctx["item_dir"], label=label)


def read_file(path):
    if path.is_symlink():
        raise ValueError("publication input must not be a symlink")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 20_000_000:
        raise ValueError("publication input must be a bounded regular file")
    return path.read_bytes()


def graphql_strings(query):
    for match in re.finditer(r'"""(?:\\.|[^\\])*?"""|"(?:\\.|[^"\\])*"', query, re.S):
        literal = match.group(0)
        if literal.startswith('"""'):
            yield literal[3:-3].replace('\\"""', '"""')
        else:
            yield json.loads(literal)


def decoded_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from decoded_strings(child)
            if key == "query" and isinstance(child, str):
                yield from graphql_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from decoded_strings(child)


def file_findings(path, ctx, *, payload=False, attachment=False, query=False):
    data = read_file(path)
    # Local source paths are not publication fields.
    label = "attachment" if attachment else "body file"
    found = text_findings("attachment name", path.name, ctx) if attachment else []
    if attachment and path.suffix.lower() in images.EXTENSIONS:
        found += images.findings(label, data, path.suffix.lower())
        if not ctx["binary_reviewed"]:
            found.append("image requires explicit per-item binary_reviewed visual review")
        return found
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return found + ["non-text attachment requires separate publication review"]
    if "\x00" in text:
        return found + ["non-text attachment requires separate publication review"]
    found += text_findings(label, text, ctx)
    if query:
        found += text_findings("GraphQL strings", "\n".join(graphql_strings(text)), ctx)
    if payload:
        try:
            value = json.loads(text)
            found += text_findings("decoded JSON", "\n".join(decoded_strings(value)), ctx)
        except (ValueError, RecursionError):
            found.append("API input must be bounded valid JSON")
    return found


def repository_write(opts, words, cwd):
    if words[:1] == ["repo"]:
        return True
    if words[:1] != ["api"] or len(words) < 2:
        return False
    endpoint = words[1]
    endpoint = (urlsplit(endpoint).path if "://" in endpoint else endpoint).split("?", 1)[0].strip("/")
    if re.fullmatch(r"(?:user|orgs/[^/]+)/repos|repos/[^/]+/[^/]+(?:/generate)?", endpoint):
        return True
    if endpoint != "graphql":
        return False
    queries = []
    for key, value in opts:
        if key == "--input":
            payload = json.loads(read_file(cwd / value))
            for request in payload if isinstance(payload, list) else [payload]:
                if isinstance(request, dict) and isinstance(request.get("query"), str):
                    queries.append(request["query"])
        elif key in {"-f", "--raw-field", "-F", "--field"}:
            field, _, content = value.partition("=")
            if field == "query":
                if key in {"-F", "--field"} and content.startswith("@"):
                    content = read_file(cwd / content[1:]).decode("utf-8")
                queries.append(content)
    for query in queries:
        source = re.sub(r'""".*?"""|"(?:\\.|[^"\\])*"|#[^\n]*', ' ', query, flags=re.S)
        source = re.sub(r"\b(?:query|mutation|subscription|fragment)\s+\w+", " ", source)
        if re.search(r"\b(?:createRepository|updateRepository|cloneTemplateRepository)\s*\(", source):
            return True
    return False


def check_gh(args, cwd, item_dir=None):
    kind = gh_kind(args)
    if kind == "read":
        return []
    if kind != "write":
        return ["unsupported gh write; use a supported explicit publication command"]
    opts, words = options(args)
    if repository_write(opts, words, cwd):
        return ["repository creation and settings writes are unsupported; run scan-repo before a separately reviewed publication"]
    api = words[0] == "api"
    family = tuple(words[:2])
    permitted = API_FLAGS if api else FLAGS.get(family, set()) | COMMON
    if any(k in INTERACTIVE or k not in permitted for k, _ in opts):
        return ["unsupported or generated publication option; supply explicit text and regular files"]
    if not api and len(words) < 2:
        return ["missing publication subcommand"]
    if not api and family[1] == "edit" and not any(k not in COMMON for k, _ in opts):
        return ["editing requires explicit non-interactive options"]
    if family[0] == "release" and len(words) < 3:
        return ["release publication requires an explicit tag"]
    if family == ("release", "upload") and len(words) < 4:
        return ["release upload requires explicit files"]
    if any(k in {"--title", "-t", "--body", "-b", "--notes", "-n"} and not (v or "").strip() for k, v in opts):
        return ["publication text must be explicit and nonempty"]
    if family in {("pr", "create"), ("issue", "create")}:
        if not any(k in {"--title", "-t"} for k, _ in opts):
            return ["creation requires an explicit title"]
    if family[0] in {"pr", "issue"} and family[1] in {"create", "comment", "review"}:
        if not any(k in BODY for k, _ in opts):
            return ["publication requires an explicit body or body file"]
    if family == ("release", "create") and not any(k in {"--notes", "-n", "--notes-file", "-F"} for k, _ in opts):
        return ["release creation requires explicit notes"]
    if family == ("repo", "create") and (len(words) != 3 or not any(k in {"--public", "--private", "--internal"} for k, _ in opts)):
        return ["repo creation requires an explicit name and visibility"]
    files, values = [], []

    def add_file(value, payload=False, attachment=False, query=False):
        if not value or value == "-":
            raise ValueError("stdin publication is unsupported; supply a regular file")
        name, sep, alt = value.partition("#") if attachment else (value, "", "")
        path = cwd / name
        files.append((path, {"payload": payload, "attachment": attachment, "query": query}))
        if sep:
            values.append(("attachment label", alt))

    for key, value in opts:
        if value is None:
            continue
        if key in FILE_FLAGS or (key == "-F" and not api):
            add_file(value, payload=api and key == "--input")
        elif key == "--attach":
            add_file(value, attachment=True)
        elif api and key in {"-F", "--field", "-f", "--raw-field"}:
            field, sep, content = value.partition("=")
            if not sep:
                return ["API fields must use key=value"]
            values.append(("field name", field))
            if key in {"-F", "--field"} and content.startswith("@"):
                add_file(content[1:], query=field == "query")
            else:
                values.append(("field value", content))
                if field == "query":
                    values.extend(("GraphQL string", value) for value in graphql_strings(content))
        elif key not in {"--source", "-s", "--remote", "--jq", "-q"}:
            values.append(("publication field", value))
    if family in {("release", "create"), ("release", "upload")}:
        for value in words[3:]:
            add_file(value, attachment=True)
        values.extend(("publication name", v) for v in words[2:3])
    elif family == ("gist", "create"):
        if len(words) < 3:
            return ["gist creation requires explicit files"]
        for value in words[2:]:
            if any(char in value for char in "#*?[]"):
                return ["gist filenames must be literal paths without # or glob patterns"]
            add_file(value, attachment=True)
    else:
        values.extend(("publication name", v) for v in words[1 if api else 2:])
    _, meta, ctx = context(item_dir, [p for p, _ in files])
    found = []
    for label, value in values:
        if api and any(token in value for token in ("{owner}", "{repo}", "{branch}")):
            found.append("API placeholders must be resolved before publication")
        found += text_findings(label, value, ctx)
    for path, file_options in files:
        found += file_findings(path, ctx, **file_options)
    if family == ("pr", "create"):
        found += pr_commits(opts, cwd, meta, ctx)
    return list(dict.fromkeys(found))


def repository_name(value):
    if github(value):
        value = urlsplit(value).path if "://" in value else value.split(":", 1)[1]
    value = value.removeprefix("github.com/").strip("/").removesuffix(".git")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", value):
        raise ValueError("PR repository must be a GitHub owner/repository")
    return value.lower()


def pr_commits(opts, cwd, meta, ctx):
    aliases = {"-H": "--head", "-B": "--base", "-R": "--repo"}
    values = {aliases.get(key, key): value for key, value in opts}
    head, base = values.get("--head"), values.get("--base")
    repo = values.get("--repo") or os.environ.get("GH_REPO")
    if not head or not base or not repo:
        return ["PR creation requires explicit --repo, --head and --base; publish the head with factory push first"]
    if os.environ.get("GH_HOST", "github.com").lower() != "github.com" or "--hostname" in values:
        return ["PR host must match the scanned GitHub repository"]
    read = lambda *args: privacy_git.git(cwd, *args).decode().strip()
    origin = read("remote", "get-url", "origin")
    if not github(origin) or repository_name(repo) != repository_name(origin):
        return ["PR repository must match origin; use a checkout of the targeted repository"]
    if ":" in head:
        owner, head = head.split(":", 1)
        if owner.lower() != repository_name(origin).split("/", 1)[0]:
            return ["external PR head is not bound to this checkout; use the item submit command"]
    oid = read("rev-parse", "--verify", "refs/heads/" + head + "^{commit}")
    base_ref = "refs/remotes/origin/" + base
    base_oid = read("rev-parse", "--verify", base_ref + "^{commit}")
    refs = remote_refs(origin, cwd)
    if refs.get("refs/heads/" + head) != oid or refs.get("refs/heads/" + base) != base_oid:
        return ["PR head or base differs from the remote; refresh local history and rerun the scan"]
    return privacy_git.scan(cwd, [oid, "^" + base_oid], names=[head, base_ref], **ctx)


def process(command, cwd, *, env=None):
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=120, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("publication preflight failed or timed out") from None
    if result.returncode:
        raise ValueError("publication preflight failed; no write attempted")
    return result.stdout


def remote_refs(url, cwd, *, env=None):
    refs = {}
    for line in process(["git", "ls-remote", "--refs", "--", url], cwd, env=env).splitlines():
        oid, sep, ref = line.partition("\t")
        if not sep or not re.fullmatch(r"[0-9a-f]{40,64}", oid):
            raise ValueError("invalid remote ref advertisement")
        refs[ref] = oid
    return refs


def push_plan(output):
    updates, url = [], None
    for line in output.splitlines():
        if line.startswith("To "):
            url = line[3:]
        elif "\t" in line:
            fields = line.split("\t")
            if len(fields) != 3 or fields[0] not in {" ", "+", "*", "-", "=", "!"} or not url:
                raise ValueError("unrecognized push update")
            flag, spec, summary = fields
            source, sep, dest = spec.partition(":")
            if flag == "!" or not sep or not dest.startswith("refs/"):
                raise ValueError("push update could not be resolved")
            updates.append((url, flag, source, dest, summary))
        elif line and line != "Done":
            raise ValueError("unrecognized push preflight output")
    if not url:
        raise ValueError("push preflight did not identify a destination")
    return updates


def tag_findings(cwd, oid, ctx):
    found = []
    kind = privacy_git.git(cwd, "cat-file", "-t", oid).decode().strip()
    while kind == "tag":
        tag = privacy_git.git(cwd, "cat-file", "tag", oid).decode("utf-8", errors="replace")
        found += text_findings("annotated tag", tag, ctx)
        oid = tag.splitlines()[0].removeprefix("object ")
        kind = privacy_git.git(cwd, "cat-file", "-t", oid).decode().strip()
    if kind != "commit":
        raise ValueError("only commit and annotated commit-tag pushes are supported")
    return oid, found


def check_push(args, cwd, item_dir=None, public=(), *, plan=None, advertised=None):
    """`public` names commits already published upstream (a fresh fetch of the PR target), so a stale
    fork does not make upstream history count as outgoing."""
    with leaks.reuse():
        return push_findings(args, cwd, item_dir, public, plan=plan, advertised=advertised)


def push_findings(args, cwd, item_dir, public, *, plan, advertised):
    _, _, ctx = context(item_dir)
    found = []
    target = push_target(args)
    urls = push_urls(args, cwd)
    if urls and all(private_destination(url) for url in urls):
        return []
    for url in urls:
        if ":" in url and not url.startswith(("/", "./", "../")):
            found += text_findings("push destination", url, ctx)
    # Scan publication operands, never local transport or item paths.
    for arg in args:
        if arg != target and not arg.startswith("--repo="):
            found += text_findings("push argument", arg, ctx)
    if found:
        return found
    plan = planned_push(args, cwd) if plan is None else plan
    advertised = {} if advertised is None else advertised
    for url, flag, source, dest, summary in plan:
        found += text_findings("outgoing refs", source + "\n" + dest, ctx)
        if flag in {"=", "-"}:
            continue
        if url not in advertised:
            advertised[url] = remote_refs(url, cwd)
        refs = advertised[url]
        old = refs.get(dest)
        if (flag == "*" and old) or (flag != "*" and not old):
            raise ValueError("remote changed during preflight; retry the scan")
        oid = privacy_git.git(cwd, "rev-parse", "--verify", source).decode().strip()
        if old:
            match = re.match(r"([0-9a-f]+)\.{2,3}([0-9a-f]+)", summary)
            if not match or not old.startswith(match[1]) or not oid.startswith(match[2]):
                raise ValueError("ref update changed during preflight; retry the scan")
        head, tag_hits = tag_findings(cwd, oid, ctx)
        found += tag_hits
        revisions = [head]
        if old:
            # Missing old objects fail closed rather than trusting a stale tracking branch.
            old_commit, _ = tag_findings(cwd, old, ctx)
            revisions.append("^" + old_commit)
        else:
            for remote_oid in set(refs.values()):
                known = privacy_git.git(cwd, "rev-parse", "--verify", remote_oid + "^{commit}", check=False).decode().strip()
                if known:
                    revisions.append("^" + known)
        revisions += ["^" + oid for oid in public]
        snapshot_context = {}
        if github(url) and repository_name(url) == CONFIG.get("public", {}).get("repository", "").lower():
            from factory.core import public as public_export

            # Trust only this installation's reviewed policy, not the outgoing manifest.
            rules = public_export.policy()
            public_export.validate_history(cwd, rules, CONFIG)
            snapshot_context["reviewed_source_lines"] = rules.get("reviewed_source_lines", {})
        found += privacy_git.scan(cwd, revisions, names=[source, dest], **ctx, **snapshot_context)
    return list(dict.fromkeys(found))


def planned_push(args, cwd, *, env=None):
    dry_args = list(args)
    boundary = dry_args.index("--") if "--" in dry_args else len(dry_args)
    dry_args[boundary:boundary] = ["--dry-run", "--porcelain"]
    return push_plan(process(["git", "push", *dry_args], cwd, env=env))


def origin_repository(cwd):
    origin = privacy_git.git(cwd, "remote", "get-url", "origin").decode().strip()
    return repository_name(origin)


def owned_repository(repo):
    return repo.split("/", 1)[0].lower() == CONFIG["github_user"].lower()


def own_project(repo, root, origin, proved):
    if repo in proved:
        return True
    try:
        if not owned_repository(repo):
            return False
        own_repositories = {name.lower() for name in CONFIG.get("own_repositories", []) if isinstance(name, str)}
        me = CONFIG["github_user"].lower()
        if repo not in own_repositories and repo != f"{me}/{me}" and not (root == ROOT.resolve() and repo == origin):
            repository = gh_json("api", f"repos/{repo}", timeout=GH_READ_TIMEOUT)
            if not isinstance(repository, dict) or repository.get("fork") is not False:
                return False
    except Exception:
        return False
    proved.add(repo)
    return True


def contribution_clone(cwd, *, proved=None):
    proved = set() if proved is None else proved
    root = pathlib.Path(privacy_git.git(cwd, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    try:
        origin = origin_repository(cwd)
        remotes = privacy_git.git(cwd, "remote").decode().splitlines()
        if "origin" not in remotes:
            return root
        repositories = set()
        for remote in remotes:
            for flags in (("--all",), ("--push", "--all")):
                urls = privacy_git.git(cwd, "remote", "get-url", *flags, remote).decode().splitlines()
                if not urls or any(not github(url) or not owned_repository(repository_name(url)) for url in urls):
                    return root
                repositories.update(repository_name(url) for url in urls)
    except (OSError, ValueError, KeyError, TypeError):
        return root
    for repo in sorted(repositories):
        if not own_project(repo, root, origin, proved):
            return root
    return None


def release_gate(item, expected):
    from factory.core import gate

    report, result = gate.run(item, with_approval=True)
    if not report.ok:
        return ["release gate failed; use bin/factory submit after fixing the gate"]
    if result["head_sha"] != expected or item.head_sha() != expected:
        return ["release gate head differs from the publication head; use bin/factory submit for the current head"]
    return []


def force_lease(args, updates, cwd, advertised):
    leases, forced, unsafe = {}, False, False
    skip_value, operands = False, False
    for arg in args:
        if operands:
            forced = forced or arg.startswith("+")
            continue
        if skip_value:
            skip_value = False
            continue
        if arg == "--":
            operands = True
            continue
        if arg in {"--repo", "--receive-pack", "--exec", "-o", "--push-option"}:
            skip_value = True
            continue
        key, _, value = arg.partition("=")
        if key == "--force-with-lease":
            forced = True
            branch, colon, sha = value.partition(":")
            branch = branch.removeprefix("refs/heads/")
            if not colon or not branch or not re.fullmatch(r"[0-9a-f]{40,64}", sha) or branch in leases:
                unsafe = True
            leases[branch] = sha
        elif arg.startswith("-o") and not arg.startswith("--"):
            continue
        elif arg.startswith("+") or key == "--force-if-includes":
            forced = True
        elif (key.startswith("--") and ("--force".startswith(key) or key.startswith("--no-force") or
                                        "--force-with-lease".startswith(key))):
            forced = unsafe = True
        elif arg.startswith("-") and not arg.startswith("--") and "f" in arg[1:]:
            forced = unsafe = True
    forced = forced or any(flag == "+" for _, flag, _, _, _ in updates)
    if not forced:
        return []
    fix = "forced contribution updates require --force-with-lease=<branch>:<sha> for the preflight remote head, never --force or -f; use bin/factory submit"
    if unsafe:
        return [fix]
    for url, _, _, dest, _ in updates:
        if url not in advertised:
            advertised[url] = remote_refs(url, cwd)
        lease = leases.get(dest.removeprefix("refs/heads/"))
        if not lease or lease != advertised[url].get(dest):
            return [fix]
    return []


def push_release(args, cwd, item_dir, *, plan=None, advertised=None):
    plan = planned_push(args, cwd) if plan is None else plan
    branches = [entry for entry in plan if entry[1] != "-" and entry[3].startswith("refs/heads/")]
    if not any(entry[1] != "=" for entry in branches):
        return []
    proved = set()
    root = contribution_clone(cwd, proved=proved)
    if root is None:
        root = pathlib.Path(privacy_git.git(cwd, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        try:
            origin = origin_repository(cwd)
            own_destinations = all(github(url) and own_project(repository_name(url), root, origin, proved)
                                   for url, _, _, _, _ in branches)
        except (OSError, ValueError, KeyError, TypeError):
            own_destinations = False
        if own_destinations:
            return []

    def refuse(found):
        guidance = ("clone treated as a contribution fork; every repository named by this clone's remotes and by the "
                    "push destination must be an own project (listed in own_repositories or a non-fork on GitHub); "
                    "otherwise use --item <item-path> for a contribution")
        return found + [guidance] if found else []

    if item_dir is None:
        return refuse(["contribution branch pushes require --item and release approval; use bin/factory submit"])
    found = force_lease(args, branches, cwd, {} if advertised is None else advertised)
    if found:
        return refuse(found)
    item = Item(item_dir)
    if item.clone.resolve() != root:
        return refuse(["--item names another clone; use bin/factory submit for this contribution"])
    expected = item.head_sha()
    def matches():
        return all(dest == "refs/heads/" + item.meta["branch"] and
                   privacy_git.git(cwd, "rev-parse", "--verify", source + "^{commit}").decode().strip() == expected
                   for _, _, source, dest, _ in branches)
    if not matches():
        return refuse(["push branches or head differ from --item; use bin/factory submit for the reviewed branch"])
    found = release_gate(item, expected)
    if not found and not matches():
        found = ["push source changed after the release gate; use bin/factory submit again"]
    return refuse(found)


def api_pr_edit(opts, words, cwd):
    if words[:1] != ["api"] or len(words) < 2:
        return False
    endpoint = words[1]
    endpoint = (urlsplit(endpoint).path if "://" in endpoint else endpoint).split("?", 1)[0].strip("/")
    payloads, queries, fields = [], [], set()
    for key, value in opts:
        if key == "--input":
            payload = json.loads(read_file(cwd / value))
            payloads.extend(payload if isinstance(payload, list) else [payload])
        elif key in {"-f", "--raw-field", "-F", "--field"}:
            name, _, content = value.partition("=")
            fields.add(name)
            if name == "query":
                if key in {"-F", "--field"} and content.startswith("@"):
                    content = read_file(cwd / content[1:]).decode("utf-8")
                queries.append(content)
    for payload in payloads:
        if isinstance(payload, dict):
            fields.update(payload)
            if isinstance(payload.get("query"), str):
                queries.append(payload["query"])
    if endpoint == "graphql":
        for query in queries:
            source = re.sub(r'""".*?"""|"(?:\\.|[^"\\])*"|#[^\n]*', " ", query, flags=re.S)
            if re.search(r"\bupdatePullRequest\s*\(", source):
                return True
        return False
    return bool(re.fullmatch(r"repos/[^/]+/[^/]+/(?:pulls|issues)/\d+", endpoint) and fields & {"body", "title"})


def gh_release(args, cwd, item_dir):
    if gh_kind(args) != "write":
        return []
    opts, words = options(args)
    fields = {key: value for key, value in opts}
    if api_pr_edit(opts, words, cwd):
        return ["API body/title edits can change a PR without release approval; use bin/factory submit (or bin/factory gh issue edit for an issue)"]
    if words[:2] == ["pr", "edit"] and "--attach" in fields:
        return ["PR attachments change the reviewed body; use bin/factory submit with an approved body file"]
    if words[:2] != ["pr", "edit"] or not any(key in BODY | {"--title", "-t"} for key in fields):
        return []
    if item_dir is None:
        return ["PR body and title edits require --item and release approval; use bin/factory submit"]
    item = Item(item_dir)
    url = item.meta.get("pr_url")
    repo = fields.get("--repo") or fields.get("-R") or os.environ.get("GH_REPO")
    selector = words[2:]
    if not url or len(selector) != 1 or not (
        selector[0].rstrip("/") == url.rstrip("/") or
        (selector[0] == url.rstrip("/").rsplit("/", 1)[-1] and repo and repository_name(repo) == repository_name(item.meta["repo"]))
    ):
        return ["PR edit must name the --item PR URL (or number and --repo); use bin/factory submit"]
    if (repo and repository_name(repo) != repository_name(item.meta["repo"])) or "--hostname" in fields or os.environ.get("GH_HOST", "github.com").lower() != "github.com":
        return ["PR edit repository or host differs from --item; use bin/factory submit"]
    from factory.core.slop import parse_pr_md
    from factory.oss.disclosure import existing_pr_body

    pr_path = os.path.relpath(item.file("pr.md"), cwd)
    reviewed_text = read_file(item.file("pr.md"))
    title, reviewed_body = parse_pr_md(reviewed_text.decode("utf-8"))
    approval = read_json(item.file("approval.json"), {})
    if approval.get("pr_sha256") != hashlib.sha256(reviewed_text).hexdigest():
        return [f"{pr_path} differs from its approval; approve the current PR text before bin/factory submit"]
    body_options = [(key, value) for key, value in opts if key in BODY]
    if body_options:
        if len(body_options) != 1 or body_options[0][0] not in {"--body-file", "-F"} or body_options[0][1] == "-":
            return ["PR body edits require one approved body file; use bin/factory submit"]
        body = cwd / body_options[0][1]
        observed = gh_json("pr", "view", url, "--json", "headRefOid,createdAt", timeout=GH_READ_TIMEOUT) or {}
        try:
            public_body = existing_pr_body(reviewed_body, read_json(item.file("policy.json"), {}), observed.get("createdAt"))
        except ValueError as error:
            return [str(error)]
        if read_file(body) != public_body.encode("utf-8"):
            return [f"PR body file differs from the rendered body in {pr_path}; use the approved body without its title and retain the factory disclosure for post-cutoff PRs"]
    titles = [value for key, value in opts if key in {"--title", "-t"}]
    if any(value != title for value in titles):
        return [f"PR title differs from {pr_path}; use its reviewed title before bin/factory submit"]
    expected = item.head_sha()
    found = release_gate(item, expected)
    if found:
        return found
    pr = gh_json("pr", "view", url, "--json", "headRefOid,createdAt", timeout=GH_READ_TIMEOUT)
    if not pr or pr.get("headRefOid") != expected:
        return ["PR head differs from the release gate head; use bin/factory submit first"]
    if body_options:
        try:
            current_body = existing_pr_body(reviewed_body, read_json(item.file("policy.json"), {}), pr.get("createdAt"))
        except ValueError as error:
            return [str(error)]
        if current_body != public_body or read_file(body) != public_body.encode("utf-8"):
            return ["PR body or disclosure policy changed after approval was checked; render it again before publishing"]
    if read_file(item.file("pr.md")) != reviewed_text:
        return ["PR text changed after approval was checked; use bin/factory submit again"]
    return []


def api_endpoint(words):
    if words[:1] != ["api"] or len(words) != 2:
        return ""
    endpoint = words[1]
    if "://" in endpoint:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or parsed.hostname != "api.github.com":
            return ""
        endpoint = parsed.path + ("?" + parsed.query if parsed.query else "")
    return endpoint.strip("/")


def gh_repositories(opts, words, cwd):
    repos = [repository_name(value) for key, value in opts if key in {"--repo", "-R"}]
    if words[:1] == ["api"]:
        match = re.match(r"repos/([^/]+/[^/]+)(?:/|$)", api_endpoint(words))
        return [repository_name(match[1])] if match else []
    for word in words[2:]:
        match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/(?:pull|issues)/\d+/?", word, re.I)
        if match:
            repos.append(repository_name(match[1]))
    if repos:
        return repos
    return [repository_name(os.environ["GH_REPO"]) if os.environ.get("GH_REPO") else origin_repository(cwd)]


def state_repair(opts, words):
    endpoint = api_endpoint(words)
    match = re.fullmatch(r"repos/([\w.-]+/[\w.-]+)/pulls/([1-9]\d*)", endpoint)
    methods = [value.upper() for key, value in opts if key in {"-X", "--method"}]
    fields = [value for key, value in opts if key in {"-f", "--raw-field", "-F", "--field"}]
    allowed = {"-X", "--method", "-f", "--raw-field", "-F", "--field", "--silent", "--jq", "-q"}
    if not (match and methods == ["PATCH"] and fields in (["state=open"], ["state=closed"]) and
            all(key in allowed for key, _ in opts) and os.environ.get("GH_HOST", "github.com").lower() == "github.com"):
        return None
    from factory.core import ledger

    url = f"https://github.com/{match[1]}/pull/{match[2]}"
    row = next((r for r in ledger.load() if (r.get("url") or "").rstrip("/").lower() == url.lower()), None)
    if row is None:
        return ["PR state repair requires one of our PRs in the ledger; recover it with bin/factory submit first"]
    pr = gh_json("pr", "view", url, "--json", "author,state", timeout=GH_READ_TIMEOUT)
    if not pr or ((pr.get("author") or {}).get("login") or "").lower() != CONFIG["github_user"].lower():
        return ["PR state repair requires our own PR; use bin/factory reply for another author's PR"]
    if pr.get("state") == "MERGED":
        return ["a merged PR cannot be repaired by closing or reopening; use bin/factory submit for new work"]
    return []


def gh_wrapper(args, cwd):
    """Return (refusals, needs_release) for generic CLI writes, separate from dedicated command protocols."""
    if gh_kind(args) == "read":
        return [], False
    opts, words = options(args)
    repos = gh_repositories(opts, words, cwd)
    if repos and all(owned_repository(repo) for repo in repos):
        return [], False
    if words[:2] == ["pr", "edit"]:
        allowed = COMMON | {"--body-file", "-F", "--title", "-t"}
        if any(key not in allowed for key, _ in opts) or not any(key not in COMMON for key, _ in opts):
            return ["contribution PR edits only support reviewed body/title; use bin/factory submit or bin/factory ship"], False
        return [], True
    repair = state_repair(opts, words)
    if repair is not None:
        return repair, False
    endpoint = api_endpoint(words)
    if words[:2] in (["pr", "comment"], ["pr", "review"]):
        command = "bin/factory reply (or bin/factory thread-reply for an inline thread)"
    elif words[:2] == ["issue", "comment"]:
        command = "bin/factory propose"
    elif words[:1] == ["issue"]:
        command = "bin/factory open-issue"
    elif "/comments" in endpoint:
        command = "bin/factory thread-reply" if "/pulls/" in endpoint else "bin/factory propose or bin/factory reply"
    elif "/issues" in endpoint:
        command = "bin/factory open-issue (PR body/title edits use bin/factory submit)"
    elif words[:1] == ["api"] and not endpoint.startswith("repos/"):
        command = "bin/factory submit / bin/factory ship, bin/factory open-issue, bin/factory propose, bin/factory reply or bin/factory thread-reply"
    else:
        command = "bin/factory submit or bin/factory ship"
    return [f"generic gh writes to contribution repositories are refused; use {command}"], False


def run(kind, args, cwd=None, item_dir=None, *, wrapper=False):
    """Scan internal publications; wrapper=True also routes generic gh writes to dedicated commands."""
    cwd = pathlib.Path(cwd or os.getcwd()).resolve()
    try:
        if (kind == "push" or gh_kind(args) != "read") and any(
            key in {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_SHALLOW_FILE"}
            or key.startswith("GIT_CONFIG_") for key in os.environ
        ):
            raise ValueError("git environment cannot be pinned; use -C without repository overrides")
        if kind == "gh":
            found, needs_release = gh_wrapper(args, cwd) if wrapper else ([], True)
            if not found:
                found = check_gh(args, cwd, item_dir)
            if not found and needs_release:
                found = gh_release(args, cwd, item_dir)
        else:
            plan, advertised = planned_push(args, cwd), {}
            found = check_push(args, cwd, item_dir, plan=plan, advertised=advertised)
            if not found and not any(entry[1] != "=" for entry in plan):
                return 0
            if not found:
                found = push_release(args, cwd, item_dir, plan=plan, advertised=advertised)
    except (OSError, ValueError, TypeError, RecursionError, KeyError, CommandError, SystemExit):
        found = ["publication scan incomplete; check explicit arguments, input files and local history"]
    if found:
        print("Publication blocked: " + "; ".join(privacy.redact(f) for f in found), file=sys.stderr)
        return 2
    command = ["gh", *args] if kind == "gh" else ["git", "push", *args]
    return subprocess.call(command, cwd=cwd)


def main(kind, args):
    start = pathlib.Path.cwd()
    cwd, item_dir = start, None
    while args:
        if args[0] == "--":
            args = args[1:]
            break
        if args[0] not in {"-C", "--item"}:
            break
        if len(args) < 2:
            print("publication wrapper requires an option value", file=sys.stderr)
            return 2
        if args[0] == "-C":
            cwd = cwd / args[1]
        else:
            # Relative to where the command was typed, not to -C (L-063).
            item_dir = (start / args[1]).resolve()
            if not (item_dir / "item.json").is_file():
                print(f"publication wrapper: --item {args[1]} is not an item directory with item.json", file=sys.stderr)
                return 2
        args = args[2:]
    if args in (["--help"], ["-h"]):
        target = "git push" if kind == "push" else "gh"
        print(f"usage: factory {kind} [-C repo] [--item item] [--] <{target} args>\n\n"
              f"Scan exact {target} arguments before publication.\n"
              "  -C repo      Run in this checkout.\n"
              "  --item item  Use this factory item's publication evidence.\n"
              "  --           Pass remaining arguments to the underlying command.")
        return 0
    if kind == "push" and item_dir is not None:
        from factory.core import push_head

        return push_head.push(args, cwd, item_dir)
    return run(kind, args, cwd, item_dir, wrapper=True)
