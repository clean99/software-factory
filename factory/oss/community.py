"""GitHub's public owner-level community health defaults, never repository instructions or workflows."""
import base64
import pathlib
import re

from factory.util import CommandError

# GitHub checks .github, root, then docs, independently for each supported file.
# https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file
FOLDERS = (".github", "", "docs")
DOCUMENTS = ("CONTRIBUTING", "CODE_OF_CONDUCT", "SECURITY", "SUPPORT", "ACCESSIBILITY")
AI_DOCUMENTS = ("AI_POLICY", "AI-POLICY", "AI")


def optional(endpoint, lookup):
    try:
        return lookup("api", endpoint)
    except CommandError as error:
        if re.search(r"\b404\b", str(error)):
            return None
        raise


def issue_override(files):
    """Only suppress defaults when a local override is certain, without a YAML parser."""
    uncertain = []
    for path, text in files.items():
        if path.endswith(".md") and not re.match(r"\A\N{ZERO WIDTH NO-BREAK SPACE}?---[ \t]*\r?\n", text):
            continue
        lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if path.endswith("/config.yml") and len(lines) == 1 and re.fullmatch(
                r"blank_issues_enabled: +(true|false)(?:[ \t]+#.*)?[ \t]*", lines[0]):
            return True
        uncertain.append(path)
    if uncertain:
        raise ValueError("unresolved issue-template override: " + ", ".join(uncertain) +
                         "; only a simple blank_issues_enabled config is validated locally. "
                         "Check the repository's GitHub issue chooser and template syntax; do not assume inherited rules are absent.")
    return False


def resolve(repo, lookup):
    owner, name = repo.split("/", 1)
    shared = owner + "/.github"
    meta = optional("repos/" + shared, lookup) if name != ".github" else None
    public_default = isinstance(meta, dict) and meta.get("private") is False
    listings, contents = {}, {}

    def read_file(source, path):
        key = source, path
        if key not in contents:
            data = optional(f"repos/{source}/contents/{path}", lookup)
            if not isinstance(data, dict) or data.get("encoding") != "base64":
                raise ValueError(f"cannot read community file {source}/{path}")
            contents[key] = base64.b64decode(data["content"]).decode("utf-8", "replace")
        return contents[key]

    def listing(source, folder):
        key = source, folder
        if key not in listings:
            data = optional(f"repos/{source}/contents/{folder}", lookup)
            if isinstance(data, dict) and data.get("type") in {"file", "submodule", "symlink"} and data.get("path") == folder:
                data = []
            if data is not None and not isinstance(data, list):
                raise ValueError(f"expected a directory listing for {source}/{folder}")
            listings[key] = data or []
        return listings[key]

    def single(source, stem, folders=FOLDERS, extensions=(".md", ".rst", ".txt")):
        for folder in folders:
            for entry in listing(source, folder):
                path = pathlib.PurePosixPath(entry["path"])
                if entry.get("type") == "file" and path.stem.upper() == stem and path.suffix.lower() in extensions:
                    return [entry["path"]]
        return []

    def templates(source, kind):
        if kind == "PULL_REQUEST_TEMPLATE":
            selected = single(source, kind)
            if selected:
                return selected
            folders = [f"{folder}/{kind}".lstrip("/") for folder in FOLDERS]
        else:
            folders = [".github/" + kind]
        for folder in folders:
            paths = [e["path"] for e in listing(source, folder) if e.get("type") == "file"
                     and pathlib.PurePosixPath(e["path"]).suffix.lower() in {".md", ".yml", ".yaml"}]
            if paths:
                return paths
        return []

    paths = []

    def select(reader, *args):
        local = reader(repo, *args)
        inherited = None
        if public_default and args == ("ISSUE_TEMPLATE",):
            inherited = reader(shared, *args)
            if inherited and not issue_override({path: read_file(repo, path) for path in local}):
                local = []
        if local:
            paths.extend((repo, path) for path in local)
        elif public_default:
            if inherited is None:
                inherited = reader(shared, *args)
            paths.extend((shared, path) for path in inherited)

    for stem in DOCUMENTS:
        select(single, stem)
    # Explicit owner automation policies apply even beside local contribution guides.
    for stem in AI_DOCUMENTS:
        paths.extend((repo, path) for path in single(repo, stem))
        if public_default:
            paths.extend((shared, path) for path in single(shared, stem))
    for kind in ("PULL_REQUEST_TEMPLATE", "ISSUE_TEMPLATE", "DISCUSSION_TEMPLATE"):
        # A valid local issue config counts as an override, even without any local form.
        select(templates, kind)
    for stem in ("FUNDING", "VULNERABILITY_REPORT"):
        select(single, stem, (".github",), (".yml", ".yaml"))
    found = {}
    for source_repo, path in paths:
        source = path if source_repo == repo else f"https://github.com/{shared}/blob/{meta['default_branch']}/{path}"
        found[source] = read_file(source_repo, path)
    return found
