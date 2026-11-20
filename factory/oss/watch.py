"""Read-only, bounded delta scans for opportunities in repos that merged our work."""
import datetime as dt
import re
from pathlib import PurePosixPath

from factory.core import ledger
from factory.oss import lanes
from factory.oss.health import CORE
from factory.oss.qualify import CLAIM
from factory.util import CONFIG, ROOT, CommandError, GH_READ_TIMEOUT, gh_json, iso, now, parse_iso, path_from_config, read_json, write_json

FIX = re.compile(r"\b(?:fix(?:es|ed)?|bug(?:fix|s)?|regression)\b", re.I)
INVITE = re.compile(r"\b(?:prs?\s+(?:are\s+)?(?:also\s+)?welcome|happy to accept|contributions?\s+(?:are\s+)?welcome|feel free to open a pr)\b", re.I)
FOLLOWUP = re.compile(r"\b(?:follow[ -]up|would also|another pr|edge case|feel free to|could you also)\b", re.I)
TRACKING = re.compile(r"\b(?:tracking|roadmap)\b", re.I)
UNCHECKED = re.compile(r"^\s*-\s+\[ \]\s+\S")
BUG = re.compile(r"\bbug\b", re.I)
BUG_AGE = dt.timedelta(hours=48)
FRESH_REASON = "younger than 48h, no maintainer invitation"
PRIORITY = {"own-followup": 0, "invite": 1, "tracking-item": 2}
HELP = ("Read-only delta scan: at most 3 GitHub calls per repo, each with a 120-second timeout. "
        "One search covers issues and merged PRs; two optional calls inspect changed files and sibling directories. "
        "Empty watch.json defaults to active depth lanes. Bugs younger than 48 hours need a maintainer invitation.")
FLAGS = re.compile(r"\b(?:bug|good[ -]first[ -]issue|help[ -]wanted|accepting[ -]prs?)\b", re.I)
REPRO = re.compile(r"https?://(?:www\.)?(?:stackblitz\.com|codesandbox\.io|codepen\.io|github\.com)/\S+|```[^`]+```", re.I | re.S)
FAMILY = re.compile(r"^(?:adapters?|providers?|plugins?|parsers?|converters?|resolvers?)$", re.I)
REF = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)|(?<![\w/])([\w.-]+/[\w.-]+)#(\d+)", re.I)
LOCAL_REF = re.compile(r"(?<![\w/#])#([1-9]\d*)\b")
REFERENCE_KEY = re.compile(r"(?:^|_)(?:pr|prs|issue|issues|number|numbers|related)(?:_|$)")
MAX_DIRECTORIES = 30
QUERY = """query($merged:String!,$issues:String!,$own:String!){
  merged:search(query:$merged,type:ISSUE,first:100){issueCount pageInfo{hasNextPage} nodes{
    ... on PullRequest{number title url author{login} authorAssociation mergedAt baseRefName labels(first:100){nodes{name}}}
  }}
  issues:search(query:$issues,type:ISSUE,first:100){issueCount pageInfo{hasNextPage} nodes{
    ... on Issue{number title url body author{login} authorAssociation state createdAt updatedAt issueType{name}
      assignees(first:1){totalCount} labels(first:100){nodes{name}}
      timelineItems(last:1,itemTypes:[REOPENED_EVENT]){nodes{... on ReopenedEvent{createdAt}}}
      references:timelineItems(last:100,itemTypes:[CROSS_REFERENCED_EVENT]){
        pageInfo{hasPreviousPage} nodes{... on CrossReferencedEvent{source{... on PullRequest{state url}}}}
      }
      comments(last:100){pageInfo{hasPreviousPage} nodes{body createdAt url authorAssociation author{login}}}
    }
  }}
  own:search(query:$own,type:ISSUE,first:100){issueCount pageInfo{hasNextPage} nodes{
    ... on PullRequest{number title url author{login} authorAssociation mergedAt labels(first:100){nodes{name}}
      comments(last:100){pageInfo{hasPreviousPage} nodes{body createdAt url authorAssociation author{login}}}
      reviews(last:100){pageInfo{hasPreviousPage} nodes{body submittedAt url authorAssociation author{login}
        comments(last:20){pageInfo{hasPreviousPage} nodes{body createdAt url authorAssociation author{login}}}
      }}
    }
  }}
}"""


def timestamp(value):
    parsed = parse_iso(value)
    if parsed.tzinfo is None:
        raise ValueError("watch timestamps need a timezone, e.g. 2026-10-28T00:00:00Z")
    return parsed.astimezone(dt.timezone.utc)


def known_items():
    known = set()

    def visit(value, repo=None, reference=False):
        if isinstance(value, dict):
            repo = value.get("repo") or repo
            for key, child in value.items():
                visit(child, repo, reference or bool(REFERENCE_KEY.search(key)))
        elif isinstance(value, list):
            for child in value:
                visit(child, repo, reference)
        elif isinstance(value, str):
            for match in REF.finditer(value):
                known.add(((match[1] or match[3]).lower(), int(match[2] or match[4])))
            if repo:
                for match in LOCAL_REF.finditer(REF.sub("", value)):
                    known.add((repo.lower(), int(match[1])))
                if reference and value.isdigit():
                    known.add((repo.lower(), int(value)))
        elif type(value) is int and reference and repo:
            known.add((repo.lower(), value))

    visit(ledger.load())
    for path in path_from_config("runs_dir").rglob("item.json"):
        visit(read_json(path))
    return known


def graphql(query, **variables):
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-f", f"{key}={value}"]
    response = gh_json(*args, timeout=GH_READ_TIMEOUT)
    if not response or response.get("errors") or not response.get("data"):
        raise CommandError("watch GraphQL read failed or returned partial data")
    return response["data"]


def labels(node):
    return [label["name"] for label in node["labels"]["nodes"]]


def metadata(node):
    return {"number": node["number"], "title": node["title"], "url": node["url"],
            "author": (node.get("author") or {}).get("login"), "author_association": node["authorAssociation"],
            "labels": labels(node)}


def directory_targets(files):
    targets = set()
    for file in files:
        path = PurePosixPath(file)
        if any(part.lower() in {"test", "tests", "__tests__", "__snapshots__", "fixtures", "docs", "examples"} for part in path.parts):
            continue
        targets.add(str(path.parent))
        targets.update(str(parent) for parent in path.parents if FAMILY.fullmatch(parent.name))
    return sorted(targets, key=lambda path: (not bool(FAMILY.fullmatch(PurePosixPath(path).name)), path))


def siblings(files, trees):
    evidence = []
    for file in files:
        path = PurePosixPath(file)
        for directory in directory_targets([file]):
            relative = path.relative_to(directory)
            child = relative.parts[0]
            kind = "tree" if len(relative.parts) > 1 else "blob"
            tokens = set(re.findall(r"[a-z0-9]+", PurePosixPath(child).stem.lower())) - {"index", "test", "spec"}
            matches = []
            for entry in trees.get(directory) or []:
                name = entry["name"]
                if name == child or entry["type"] != kind:
                    continue
                similar = tokens & set(re.findall(r"[a-z0-9]+", PurePosixPath(name).stem.lower()))
                if FAMILY.fullmatch(PurePosixPath(directory).name) or (similar and PurePosixPath(name).suffix == path.suffix):
                    matches.append(str(PurePosixPath(directory) / name))
            if len(matches) >= 2:
                evidence.append({"file": file, "directory": directory, "siblings": sorted(matches)[:5]})
    return evidence


def candidate(kind, repo, node, score, why, observed_at, source=None, quote=None, **extra):
    return {"kind": kind, "repo": repo, **metadata(node), "score": score, "why": why,
            "source": source or node["url"], "quote": quote if quote is not None else node.get("body") or node["title"],
            "observed_at": iso(observed_at), **extra}


def rank(item):
    return (PRIORITY.get(item["kind"], 3), -item["score"], item.get("tier", 1), item["repo"].lower(), item["number"])


def maintainer(comment, me):
    return comment.get("authorAssociation") in CORE and (comment.get("author") or {}).get("login", "").lower() != me


def thread_nodes(thread, since, name, limit=100):
    nodes = [node for node in thread["nodes"] if node]
    if thread["pageInfo"]["hasPreviousPage"] and (not nodes or timestamp(nodes[0]["createdAt"]) > since):
        raise CommandError(f"watch {name} comments exceed the {limit}-result budget; cursor retained")
    return nodes


def is_bug(node):
    return any(BUG.search(value) for value in labels(node) + [(node.get("issueType") or {}).get("name", "")])


def tracking_lines(node):
    if node["authorAssociation"] not in CORE or not any(TRACKING.search(value) for value in [node["title"]] + labels(node)):
        return []
    lines, fence = [], None
    for number, line in enumerate((node.get("body") or "").splitlines(), 1):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        elif fence is None and UNCHECKED.match(line):
            lines.append((number, line))
    return lines


def issue_candidates(repo, node, since, until, me):
    created = timestamp(node["createdAt"])
    bug = is_bug(node)
    fresh = bug and until - created < BUG_AGE
    cutoff = min(since, created) if fresh else since
    comments = thread_nodes(node["comments"], cutoff, f'issue #{node["number"]}')
    references = node["references"]
    if references["pageInfo"]["hasPreviousPage"]:
        raise CommandError(f'watch issue #{node["number"]} references exceed the 100-result budget; cursor retained')
    if any((event.get("source") or {}).get("state") == "OPEN" for event in references["nodes"] if event):
        return [], []
    if any(CLAIM.search(comment.get("body") or "") and until - dt.timedelta(days=30) < timestamp(comment["createdAt"]) <= until
           for comment in comments):
        return [], []
    invitations = [comment for comment in comments if maintainer(comment, me)
                   and INVITE.search(comment.get("body") or "") and timestamp(comment["createdAt"]) <= until]
    events = [created] + [timestamp(event["createdAt"]) for event in node["timelineItems"]["nodes"] if event]
    if bug:
        events.append(created + BUG_AGE)
    changed = any(since < event <= until for event in events)
    tracking = tracking_lines(node) if changed or since < timestamp(node["updatedAt"]) <= until else []
    fresh_invites = [comment for comment in invitations if since < timestamp(comment["createdAt"])]
    if not changed and not tracking and not fresh_invites:
        return [], []

    flags = [label for label in labels(node) if FLAGS.search(label)]
    repro = bool(REPRO.search(node.get("body") or ""))
    candidates = [candidate("tracking-item", repo, node, 250, "unchecked item in a maintainer tracking issue", until,
                            quote=line, line=number) for number, line in tracking]
    if changed and not candidates:
        if bug and repro:
            score = 200
        elif bug:
            score = 120
        elif flags:
            score = 100
        else:
            score = 50
        why = "unassigned bug with a reproduction link or code sample" if bug and repro else "open, unassigned; " + (", ".join(flags) or "new or reopened")
        candidates.append(candidate("issue", repo, node, score, why, until, flags=flags, has_repro=repro))
    if fresh and not invitations:
        return [], [{**item, "reason": FRESH_REASON} for item in candidates]
    if fresh_invites or (changed and invitations):
        comment = max(fresh_invites or invitations, key=lambda item: timestamp(item["createdAt"]))
        candidates = [item for item in candidates if item["kind"] != "issue"]
        candidates.append(candidate("invite", repo, node, 300, f'maintainer: {INVITE.search(comment["body"])[0]}', until,
                                    source=comment["url"], quote=comment["body"], comment_url=comment["url"], comment_at=comment["createdAt"],
                                    comment_author=(comment.get("author") or {}).get("login"),
                                    comment_author_association=comment["authorAssociation"]))
    return candidates, []


def own_followups(repo, node, since, until, me):
    comments = thread_nodes(node["comments"], since, f'PR #{node["number"]}')
    reviews = node["reviews"]
    if reviews["pageInfo"]["hasPreviousPage"]:
        raise CommandError(f'watch PR #{node["number"]} reviews exceed the 100-result budget; cursor retained')
    for review in reviews["nodes"]:
        if not review or not review.get("submittedAt"):
            continue
        comments.append({**review, "createdAt": review["submittedAt"]})
        comments.extend(thread_nodes(review["comments"], since, f'PR #{node["number"]} review', limit=20))
    candidates = []
    for comment in comments:
        match = FOLLOWUP.search(comment.get("body") or "")
        if match and maintainer(comment, me) and since < timestamp(comment["createdAt"]) <= until:
            candidates.append(candidate("own-followup", repo, node, 400, f"maintainer named remaining work: {match[0]}", until,
                                        source=comment["url"], quote=comment["body"], merged_at=node["mergedAt"],
                                        comment_url=comment["url"], comment_at=comment["createdAt"],
                                        comment_author=(comment.get("author") or {}).get("login"),
                                        comment_author_association=comment["authorAssociation"]))
    return candidates


def scan(repo, since, until, known):
    start = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    me = CONFIG["github_user"].lower()
    issue_start = (since - BUG_AGE).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = graphql(QUERY, merged=f"repo:{repo} is:pr is:merged merged:>={start} -author:{me}",
                   issues=f"repo:{repo} is:issue is:open no:assignee -linked:pr updated:>={issue_start}",
                   own=f"repo:{repo} is:pr is:merged author:{me} updated:>={start}")
    for name in ("merged", "issues", "own"):
        if data[name]["pageInfo"]["hasNextPage"] or data[name]["issueCount"] > 100:
            raise CommandError(f"watch {name} exceeds the 100-result budget; cursor retained")

    def unseen(node):
        return (repo.lower(), node["number"]) not in known and (node.get("author") or {}).get("login", "").lower() != me

    merged = [node for node in data["merged"]["nodes"] if node and unseen(node) and since < timestamp(node["mergedAt"]) <= until]
    issues = {node["number"]: node for node in data["issues"]["nodes"]
              if node and unseen(node) and node["state"] == "OPEN" and node["assignees"]["totalCount"] == 0}
    own = [node for node in data["own"]["nodes"] if node and node.get("mergedAt") and timestamp(node["mergedAt"]) <= until
           and (node.get("author") or {}).get("login", "").lower() == me]
    candidates, rejected = [], []
    for node in issues.values():
        found, refused = issue_candidates(repo, node, since, until, me)
        candidates.extend(found)
        rejected.extend(refused)
    for node in own:
        candidates.extend(own_followups(repo, node, since, until, me))

    fixes = [node for node in merged if FIX.search(node["title"]) or any(FIX.search(label) for label in labels(node))]
    trees, changes = {}, {}
    if fixes:
        owner, name = repo.split("/")
        fields = " ".join(f'p{node["number"]}:pullRequest(number:{node["number"]}){{files(first:100){{pageInfo{{hasNextPage}} nodes{{path}}}}}}' for node in fixes)
        details = graphql("query($owner:String!,$name:String!){repository(owner:$owner,name:$name){" + fields + "}}", owner=owner, name=name)["repository"]
        for node in fixes:
            files = details[f'p{node["number"]}']["files"]
            if files["pageInfo"]["hasNextPage"]:
                raise CommandError(f'watch PR #{node["number"]} exceeds the 100-file budget; cursor retained')
            changes[node["number"]] = [file["path"] for file in files["nodes"]]
        targets = sorted({(node["baseRefName"], directory) for node in fixes for directory in directory_targets(changes[node["number"]])},
                         key=lambda pair: (not bool(FAMILY.fullmatch(PurePosixPath(pair[1]).name)), pair))
        selected = targets[:MAX_DIRECTORIES]
        if selected:
            fields = " ".join(f'd{i}:object(expression:$d{i}){{... on Tree{{entries{{name type}}}}}}' for i in range(len(selected)))
            variables = {f"d{i}": f"{branch}:{'' if directory == '.' else directory}" for i, (branch, directory) in enumerate(selected)}
            declarations = ",".join(f"$d{i}:String!" for i in range(len(selected)))
            tree_data = graphql("query($owner:String!,$name:String!," + declarations + "){repository(owner:$owner,name:$name){" + fields + "}}",
                                owner=owner, name=name, **variables)["repository"]
            trees = {target: (tree_data[f"d{i}"] or {}).get("entries", []) for i, target in enumerate(selected)}
        for node in fixes:
            files = changes[node["number"]]
            evidence = siblings(files, {directory: entries for (branch, directory), entries in trees.items() if branch == node["baseRefName"]})
            why = f'merged fix; sibling paths in {evidence[0]["directory"]}' if evidence else "merged fix; check sibling paths"
            candidates.append(candidate("sibling-fix", repo, node, 150 if evidence else 70, why, until,
                                        merged_at=node["mergedAt"], files=files, siblings=evidence))
    return {"merged_prs": [{**metadata(node), "merged_at": node["mergedAt"], **({"files": changes[node["number"]]} if node["number"] in changes else {})} for node in merged],
            "own_merged_prs": [{**metadata(node), "merged_at": node["mergedAt"]} for node in own],
            "candidates": sorted(candidates, key=rank), "rejected": rejected, "sibling_directories_checked": len(trees),
            "sibling_directories_omitted": max(0, len(targets) - MAX_DIRECTORIES) if fixes else 0}


def run(repos=None, since=None, dry_run=False):
    until = now().replace(microsecond=0)
    override = timestamp(since) if since else None
    if override and override >= until:
        raise ValueError("--since must be before the scan start")
    watchlist = read_json(ROOT / "lines" / "oss" / "watch.json")
    if isinstance(watchlist, dict):
        watchlist = watchlist.get("repos")
    if not watchlist:
        watchlist = [{"repo": repo, "tier": 1} for repo in lanes.order()]
    watchlist = [{"tier": 1, **({"repo": entry} if isinstance(entry, str) else entry)} for entry in watchlist]
    selected = {repo.lower() for repo in repos or []}
    available = {entry["repo"].lower() for entry in watchlist}
    if selected - available:
        raise ValueError("repos not in watch list: " + ", ".join(sorted(selected - available)))
    watchlist = [entry for entry in watchlist if not selected or entry["repo"].lower() in selected]
    state_path = path_from_config("ledger_file").with_name("watch-state.json")
    state = read_json(state_path, {})
    known = known_items()
    report = {"at": iso(until), "dry_run": dry_run, "repos": [], "candidates": [], "rejected": []}
    successful = {}
    for entry in watchlist:
        repo = entry["repo"]
        row = {**entry, "until": iso(until)}
        try:
            saved = state.get(repo.lower())
            cursor = override or (timestamp(saved) if saved else until - dt.timedelta(hours=48))
            row["since"] = iso(cursor)
            if cursor > until:
                raise ValueError("saved cursor is after scan start")
            result = scan(repo, cursor, until, known)
            row.update(status="ok", **result)
            successful[repo.lower()] = iso(until)
            report["candidates"].extend({**item, "tier": entry["tier"]} for item in result["candidates"])
            report["rejected"].extend({**item, "tier": entry["tier"]} for item in result["rejected"])
        except Exception as error:
            row.update(status="error", error=" ".join(str(error).split()))
        report["repos"].append(row)
    report["candidates"].sort(key=rank)
    local = until.astimezone()
    output = path_from_config("runs_dir") / local.strftime("%Y-%m-%d") / "watch" / (local.strftime("%H%M") + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    report["output"] = "/".join(output.parts[-4:])
    write_json(output, report)
    if successful and not dry_run:
        with ledger.locked():
            current = read_json(state_path, {})
            for repo, cursor in successful.items():
                if repo not in current or timestamp(cursor) > timestamp(current[repo]):
                    current[repo] = cursor
            temporary = state_path.with_suffix(".tmp")
            write_json(temporary, current)
            temporary.replace(state_path)
    return report


def summary(report):
    lines = []
    for row in report["repos"]:
        if row["status"] == "error":
            lines.append(f'{row["repo"]}: ERROR {row["error"]}')
        else:
            counts = {kind: sum(item["kind"] == kind for item in row["candidates"])
                      for kind in ("sibling-fix", "issue", "invite", "own-followup", "tracking-item")}
            counts["rejected"] = len(row.get("rejected", []))
            lines.append(f'{row["repo"]}: ' + ", ".join(f"{count} {kind}" for kind, count in counts.items()))
    for item in report["candidates"][:10]:
        title = " ".join(item["title"].split())
        lines.append(f'{item["kind"]} {item["repo"]}#{item["number"]} {title} — {item["why"]}')
    lines.append(f'JSON: {report["output"]}' + (" (dry-run; cursors unchanged)" if report["dry_run"] else ""))
    return lines
