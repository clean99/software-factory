"""The gate decides whether an item may leave the factory.

It trusts nothing it did not compute itself at the current HEAD: self-reported verdicts must be
backed by files, review and red→green results must name the exact sha they examined, and the
diff hygiene and issue-freshness checks are re-run on every call.
"""
import fnmatch
import re

from factory.core import checks, slop, verdicts
from factory.core.verdicts import VERDICT, REVIEWER, REVIEWED_SHA, recommitted
from factory.util import CONFIG, CommandError, RateLimited, gh_json, gh_paginate, git, hours_since, iso, issue_number, read_json, run as sh, write_json

WHO_HITS = re.compile(r"^\W*who[ -]hits[ -]it\b[^:]*:\s*(\S.{9,})", re.I | re.M)
IMPACT = re.compile(r"^\W*(?:user[ -])?impact\b[^:]*:\s*(\S.{9,})", re.I | re.M)


class Report:
    def __init__(self):
        self.rows = []

    def add(self, name, ok, detail="", fix=""):
        self.rows.append({"check": name, "ok": ok, "detail": detail, "fix": fix})

    def warn(self, name, detail):
        self.rows.append({"check": name, "ok": True, "warn": True, "detail": detail})

    @property
    def ok(self):
        return all(r["ok"] for r in self.rows)

    def text(self):
        out = []
        for r in self.rows:
            mark = "PASS" if r["ok"] and not r.get("warn") else "WARN" if r["ok"] else "FAIL"
            out.append(f"[{mark}] {r['check']}: {r['detail']}")
            if not r["ok"] and r.get("fix"):
                out.append(f"       fix: {r['fix']}")
        return "\n".join(out)


def model_of(name):
    return (name or "").strip().split()[0].strip("()[],;").lower() if (name or "").strip() else ""


def review_independence(reviewer, implementer, kind):
    """ok: a different model reviewed. warn: same model in a declared fresh context (small items only). fail: otherwise."""
    if not reviewer:
        return "fail", "no reviewer recorded"
    if model_of(reviewer) != model_of(implementer):
        return "ok", ""
    if kind == "large":
        return "fail", "large items need a reviewer from a different model"
    if "fresh" in reviewer.lower():
        return "warn", "same model as the implementer, fresh context only"
    return "fail", "the implementer's model reviewed its own work"


def independence_names(item, reviewer):
    """Reviewer and implementer for the independence check; models the transcripts show replace names a worker typed."""
    from factory.core import provenance

    recorded = provenance.models(item)
    implementer = recorded.get("implement") or (item.meta.get("implementer") or "").strip()
    actual = recorded.get("review")
    if actual and model_of(reviewer or "") != model_of(actual):
        reviewer = f"{actual} (fresh context; model from the transcript, review.json names {reviewer or 'nobody'})"
    return reviewer, implementer


def user_impact_missing(path):
    """Names of the user-impact lines verify.md lacks (L-061, owner 2026-11-02: only changes with a positive effect go out)."""
    text = verdicts.markdown_text(path) or ""
    return [name for name, pattern in (("a who-hits-it line", WHO_HITS), ("an impact line", IMPACT)) if not pattern.search(text)]


def verdict_of(path):
    return (verdicts._markdown(path) or {}).get("verdict")


def independent_review_checks(item, rep, head):
    try:
        review = verdicts.read_review(item)
    except verdicts.EvidenceError as error:
        rep.add("independent review", False, str(error), "repair the review file and re-review")
        return
    if review is None:
        name = "review.json" if item.schema >= 2 else "review.md"
        rep.add("independent review", False, f"{name} missing",
                f"run the reviewer brief and record the review in {name}")
        return
    verdict, reviewer, reviewed_head = review["verdict"], review["reviewer"], review["head"]
    reviewer, implementer = independence_names(item, reviewer)
    independence, why = review_independence(reviewer, implementer, item.meta.get("kind"))
    detail = f"reviewer={reviewer or '?'} implementer={implementer or '?'} verdict={verdict}"
    if verdict == "ship" and independence == "warn":
        rep.warn("independent review", f"{detail}; {why}")
    else:
        if independence != "ok":
            detail += f"; {why}"
        rep.add("independent review", verdict == "ship" and independence == "ok", detail,
                "get a review from a different model (required for kind=large) or, for small items, a fresh-context agent that only sees the diff; record reviewer, head and verdict")
    same = verdicts.bound(item, head, reviewed_head)
    note = f"reviewed {reviewed_head or '?'} vs head {head[:12]}"
    if same and not head.startswith(reviewed_head):
        note += " (re-committed with identical tree and message)"
    rep.add("review is of this HEAD", same, note, "the code changed after review; review again and record the new head sha")


def core_checks(item, rep, head, mode="review"):
    from factory.util import global_hooks_drift

    drift = global_hooks_drift()
    rep.add("machine hook files unchanged", not drift, ", ".join(drift) or "global hooks match the recorded baseline",
            "a tool rewrote the machine's global git hooks (lefthook/husky); restore them from the *.old copies, tell the owner, and set LEFTHOOK=0 HUSKY=0 for installs (L-035)")
    local_hooks = git(item.clone, "config", "--local", "--get", "core.hooksPath", check=False).strip()
    rep.add("machine commit hooks active", not local_hooks, f"clone overrides core.hooksPath with {local_hooks}" if local_hooks else "no clone-local hooksPath override",
            "remove the clone-local override (git config --local --unset core.hooksPath) and recommit so the machine's hooks run; use HUSKY=0 for installs (L-035)")
    meta = item.meta
    if item.schema >= 2:
        required = ["pr.md", "verify.md", "interview.md"]
    else:
        required = ["pr.md", "validation.md", "verify.md"]
        if mode != "prepare":
            required.append("review.md")
        if meta.get("line") == "oss":
            required.append("interview.md")
    if meta.get("kind") == "large":
        required.append("design.md")
    missing = [f for f in required if not item.file(f).exists()]
    rep.add("artifacts", not missing, "all present" if not missing else f"missing {', '.join(missing)}",
            "write the missing files (templates in lines/<line>/templates/)")

    try:
        v = (verdicts.read_self_review(item) or {}).get("verdict")
        rep.add("self-review", v == "ship", f"verify.md verdict={v}", "finish the adversarial self-review; fix until verdict: ship")
    except verdicts.EvidenceError as error:
        rep.add("self-review", False, str(error), "repair verify.md and re-review")
    if item.schema < 2 and meta.get("line") == "oss" and not meta.get("pr_url"):
        try:
            missing_lines = user_impact_missing(item.file("verify.md"))
            rep.add("user impact stated", not missing_lines,
                    "who hits it and impact are stated" if not missing_lines else f"verify.md lacks {' and '.join(missing_lines)}",
                    "add 'Who hits it: <open report, captured real output, or documented usage>' (L-061) and "
                    "'Impact: <what users see change once it is fixed>' to verify.md; if neither can be named, drop the item")
        except verdicts.EvidenceError as error:
            rep.add("user impact stated", False, str(error), "repair verify.md and re-review Who hits it/Impact")

    if mode != "prepare":
        independent_review_checks(item, rep, head)

    if item.file("restoration.json").exists():
        rep.add("red→green", False, "source restoration is pending", "inspect restoration.json and restore the clone before rerunning red→green")
    elif meta.get("no_test_reason"):
        rep.warn("red→green", f"skipped: {meta['no_test_reason']}")
    else:
        rg = read_json(item.file("redgreen.json"))
        if not rg:
            rep.add("red→green", False, "never run", "bin/factory redgreen <item>")
        else:
            fresh = rg.get("head_sha") == head
            same_tree = not fresh and bool(rg.get("head_sha")) and recommitted(item.clone, rg["head_sha"], head)
            complete = rg.get("state", "complete") == "complete"
            rep.add("red→green", rg.get("ok") is True and (fresh or same_tree) and complete,
                    f"red_exit={rg.get('red_exit')} green_exit={rg.get('green_exit')} at {str(rg.get('head_sha', '?'))[:12]}"
                    + ("" if fresh else " (re-committed with identical tree and message)" if same_tree else " (stale)") + ("" if complete else " (incomplete)"),
                    "the test must fail without the fix and pass with it, on the current head; rerun bin/factory redgreen")

    s = slop.check(item)
    write_json(item.file("slop.json"), s)
    rep.add("hygiene", s["ok"], f"{s['stats'].get('src_lines', 0)} source + {s['stats'].get('test_lines', 0)} test lines, {s['stats']['files']} files" + (": " + "; ".join(s["fails"][:6]) if s["fails"] else ""),
            "fix each listed item; do not add exceptions to item.json without a reason a maintainer would accept")
    for w in s["warns"][:8]:
        rep.warn("hygiene", w)

    base = item.base_ref
    behind = int(git(item.clone, "rev-list", "--count", f"{meta['branch']}..{base}") or 0)
    merge = sh(["git", "-C", str(item.clone), "merge-tree", "--write-tree", base, meta["branch"]], check=False)
    rep.add("merges cleanly", merge.returncode == 0, f"{behind} commits behind {base}",
            f"git rebase {base} and rerun tests, red→green and review")
    if behind > 200:
        rep.warn("freshness", f"{behind} commits behind; rebase before submitting")


def fetch_issue(repo, number):
    """Live precondition evidence; kept separate so gates can be tested offline."""
    return gh_json("api", f"repos/{repo}/issues/{number}")


CLOSING_REFERENCE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)(?:\s*:\s*|\s+)"
    r"(?:https://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/issues/|(?:(?P<repo>[\w.-]+/[\w.-]+))?#)"
    r"(?P<number>[1-9]\d*)(?=$|[\s.,;:!?)\]])", re.I,
)


def closing_references(body):
    body = re.sub(r"<!--.*?-->|(?m:^```[^\n]*\n).*?(?m:^```[^\n]*$)", "", body or "", flags=re.S)
    return list(CLOSING_REFERENCE.finditer(body))


def closes_issue(text, repo, number):
    _, body = slop.parse_pr_md(text)
    return any(int(m["number"]) == number and (m["url_repo"] or m["repo"] or repo).lower() == repo.lower()
               for m in closing_references(body))


def precedent_checks(item, rep):
    from factory.oss.followup import response_kind

    repo, ref = item.meta["repo"], item.meta.get("precondition_met")
    fix = (f"set precondition_kind to 'precedent' and precondition_met to a PR URL in {repo} with no closing-keyword issue link; "
           "it must be merged or have an OWNER/MEMBER/COLLABORATOR approval or a changes-requested review with edits")
    try:
        match = re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/([1-9]\d*)/?", str(ref or ""), re.I)
        if not match:
            raise ValueError(f"precedent must be a PR URL in {repo}")
        number = int(match[1])
        pr = gh_json("api", f"repos/{repo}/pulls/{number}")
        if not pr or (pr.get("html_url") or "").rstrip("/").lower() != str(ref).rstrip("/").lower():
            raise ValueError("the reference did not resolve to the specified PR")
        if closing_references(pr.get("body")):
            raise ValueError("precedent PR has a closing-keyword issue link")
        evidence = "merged" if pr.get("merged_at") else ""
        if not evidence and pr.get("state") == "closed":
            raise ValueError("precedent PR was closed without merging")
        if not evidence:
            reviews = gh_paginate(f"repos/{repo}/pulls/{number}/reviews?per_page=100")
            for review in reviews:
                if response_kind({**review, "author": review.get("user"), "authorAssociation": review.get("author_association")}) != "maintainer":
                    continue
                state = review.get("state")
                if state == "APPROVED":
                    evidence = "maintainer approval"
                    break
                if state == "CHANGES_REQUESTED":
                    edits = (review.get("body") or "").strip()
                    if not edits:
                        comments = gh_paginate(f"repos/{repo}/pulls/{number}/reviews/{review['id']}/comments?per_page=100")
                        edits = any((c.get("body") or "").strip() for c in comments)
                    if edits:
                        evidence = "maintainer requested edits"
                        break
        if not evidence:
            raise ValueError("precedent PR is not merged and has no qualifying maintainer review")
    except (CommandError, ValueError) as error:
        rep.add("precedent precondition evidence", False, str(error), fix)
        return
    rep.add("precedent precondition evidence", True, f"{ref}: {evidence}; no closing-keyword issue link")


def precondition_checks(item, rep, flags):
    from factory.oss.policy import ISSUE_REQUIREMENTS

    meta, repo = item.meta, item.meta["repo"]
    linked = "linked_issue_enforced" in flags
    if meta.get("precondition_kind") == "precedent":
        allowed = not linked and "claim_first" in flags and meta.get("issue_first") != "issue"
        if not allowed:
            rep.add("precedent precondition evidence", False, "precedent cannot replace this repository's issue requirement",
                    "use live issue evidence; precedent is only available for claim_first without linked_issue_enforced or an issue-first decision")
            return
        precedent_checks(item, rep)
        return
    if not (linked or "claim_first" in flags or ("repro_required" in flags and meta.get("issue_first") == "issue")):
        return
    if linked and flags.get("linked_issue_docs_exempt") and not meta.get("issue"):
        globs = flags["linked_issue_docs_exempt"][0].rsplit("globs=", 1)[-1].split(",")
        changed = [f for f in git(item.clone, "diff", "--name-only", f"{item.merge_base()}..{meta['branch']}").splitlines() if f]
        if changed and all(any(fnmatch.fnmatchcase(f, g) for g in globs) for f in changed):
            rep.add("issue precondition evidence", True, f"docs-only change ({len(changed)} files) is exempt under the repository's workflow")
            return
    ref = meta.get("issue")
    if not ref and "claim_first" in flags and not linked and meta.get("issue_first") != "issue":
        ref = meta.get("precondition_met")
    fix = f"open an issue in {repo} with a reproduction, set item.json issue to its number, and keep it open before submitting"
    if "claim_first" in flags and not linked and meta.get("issue_first") != "issue":
        fix += "; or set precondition_kind: precedent and precondition_met to a same-repo, issue-less PR merged or reviewed by a maintainer"
    try:
        number = issue_number(ref, repo)
        issue = fetch_issue(repo, number)
        if not issue or "pull_request" in issue or issue_number(issue.get("html_url"), repo) != number:
            raise ValueError("the reference did not resolve to an issue in this repository")
        if issue.get("state") != "open":
            raise ValueError(f"issue #{number} is {issue.get('state')}, not open")
    except (CommandError, ValueError) as error:
        rep.add("issue precondition evidence", False, str(error), fix)
        return
    rep.add("issue precondition evidence", True, issue["html_url"] + " is open")
    if not linked:
        return
    evidence = "\n".join(flags["linked_issue_enforced"])
    if "linked_issue_closing_keyword" in flags or ISSUE_REQUIREMENTS["linked_issue_closing_keyword"].search(evidence):
        text = item.file("pr.md").read_text() if item.file("pr.md").exists() else ""
        rep.add("issue closing keyword", closes_issue(text, repo, number), f"pr.md must close {repo}#{number}",
                f"add 'Fixes #{number}' (or Closes/Resolves) to the PR body, not just a bare issue link")
    if "linked_issue_assignment" in flags or ISSUE_REQUIREMENTS["linked_issue_assignment"].search(evidence):
        assignees = [a["login"] for a in issue.get("assignees", [])]
        me = CONFIG["github_user"]
        issue_author = (issue.get("user") or {}).get("login", "")
        exempt = bool(flags.get("linked_issue_assignment_author_exempt")) and issue_author.lower() == me.lower()
        rep.add("issue assigned to author", exempt or me.lower() in [a.lower() for a in assignees],
                f"#{number} issue author {me} is exempt under the workflow" if exempt else f"#{number} assigned to {', '.join(assignees) or 'nobody'}",
                f"wait for the issue to be assigned to {me}; setting precondition_met does not establish assignment")


def oss_checks(item, rep):
    from factory.oss import policy, qualify

    meta = item.meta
    q = read_json(item.file("qualify.json"))
    if not q or hours_since(q["at"]) > CONFIG["limits"]["qualify_max_age_hours"]:
        try:
            q = qualify.run(meta["repo"], meta.get("issue"), meta.get("keywords", []), meta.get("target", "issue"))
        except RateLimited as error:
            # Quota paces the work; it says nothing about the issue, so nothing is saved and nothing suggests dropping it.
            rep.add("issue still free", False, f"not checked: {error}",
                    "wait for the GitHub reset shown and rerun; this is not a reason to drop or park the item")
            q = None
        except (CommandError, ValueError) as error:
            q = {"verdict": "skip", "at": iso(), "skip": [f"qualification lookup failed: {error}; check the issue reference and retry"]}
        else:
            write_json(item.file("qualify.json"), q)
    if q is not None:
        rep.add("issue still free", q["verdict"] == "ok", "; ".join(q.get("skip", [])) or f"checked {q['at']}",
                "someone else owns it now; drop the item or coordinate on the issue")
        for w in q.get("warn", [])[:5]:
            rep.warn("issue", w)

    p, replaced = policy.for_item(read_json(item.file("policy.json")), meta["repo"])
    if replaced:
        write_json(item.file("policy.json"), p)
    flags = p.get("flags", {})
    rep.add("outside contributors accepted", "collaborators_only" not in flags, "; ".join(flags.get("collaborators_only", [])[:2]) or "no collaborator-only rule found",
            "the repo only takes PRs from collaborators; drop the item or offer the finding as an issue")
    blocked, endorsed = policy.blockers(p), policy.endorsed(p)
    if blocked:
        detail = "; ".join(blocked)
    elif endorsed:
        detail = f"the owner's standing endorsement covers {len(endorsed)} AI-policy rule(s)"
    else:
        detail = "no automation restriction found"
    rep.add("AI policy allows it", not blocked, detail,
            "drop repos that ban or restrict automated contributions beyond what the owner's recorded endorsement covers")
    if not meta.get("pr_url"):
        from factory.oss.disclosure import new_pr_body
        from factory.core.slop import parse_pr_md

        try:
            new_pr_body(parse_pr_md(item.file("pr.md").read_text())[1], p)
            problem = ""
        except (OSError, ValueError) as error:
            problem = str(error)
        rep.add("AI disclosure prepared", not problem, problem or "ship adds the repository format or factory disclosure",
                "read the quoted disclosure rule and support its format before shipping")
    precondition_checks(item, rep, flags)
    if "repro_required" in flags and not meta.get("pr_url"):
        choice = meta.get("issue_first")
        decided = (choice == "issue" and bool(meta.get("issue"))) or (choice == "direct" and bool((meta.get("issue_first_note") or "").strip()))
        rep.add("issue first decided", decided, f"issue_first={choice}: " + "; ".join(flags["repro_required"][:2]),
                "the bug form requires a reproduction (L-036): open the issue with a runnable reproduction and set issue_first: issue with issue, "
                "or set issue_first: direct with issue_first_note citing recent outsider bug-fix merges in this repo that had no issue")
    if "human_prose" in flags:
        not_applicable = (meta.get("human_prose_not_applicable") or "").strip()
        rep.add("PR text by the owner", bool(meta.get("prose_by_owner")) or bool(not_applicable),
                not_applicable or "; ".join(flags["human_prose"][:2]),
                "the repo wants descriptions and replies written by the person: the owner rewrites pr.md in their own words, then set prose_by_owner: true")
    if "cla" in flags:
        rep.warn("CLA", "repo requires a CLA/DCO; the user signs it on the first PR")
        if any(re.search(r"signed-off-by|--signoff|git commit -s\b|\bDCO\b|Developer Certificate of Origin", line, re.I) for line in flags["cla"]):
            bodies = git(item.clone, "log", "--format=%B%x00", f"{item.merge_base()}..{meta['branch']}").split("\x00")
            unsigned = [b for b in bodies if b.strip() and not re.search(r"^Signed-off-by: .+<.+>$", b, re.M)]
            rep.add("DCO sign-off", not unsigned, f"{len(unsigned)} commit(s) without Signed-off-by" if unsigned else "every commit is signed off",
                    "the repo requires DCO sign-off, which the owner allowed (L-078); add it with git commit --amend --signoff under the factory-owner noreply identity")
    if "changeset" in flags:
        changed = git(item.clone, "diff", "--name-only", f"{item.merge_base()}..{meta['branch']}")
        if ".changeset/" not in changed:
            rep.warn("changeset", "repo mentions changesets but the diff has none")


def coordinator_check(item, rep, head):
    """Require the coordinator's verdict for this head and, on schema 2, this PR text."""
    name = "coordinator-review.json" if item.schema >= 2 else "coordinator-review.md"
    try:
        review = verdicts.read_coordinator(item)
    except verdicts.EvidenceError as error:
        rep.add("coordinator review", False, str(error), f"repair {name} and re-review")
        return
    if review is None:
        rep.add("coordinator review", False, f"{name} missing",
                f"read the full diff, tests and evidence, rerun the key reproduction at base and head, then write {name}")
        return
    sha, rv = review["head"], review["verdict"]
    same = verdicts.bound(item, head, sha)
    detail = f"verdict={rv} reviewed {sha or '?'} vs head {head[:12]}"
    if item.schema >= 2:
        pr_hash = verdicts.pr_text_sha256(item)
        text_matches = bool(pr_hash) and review["pr_sha256"] == pr_hash
        same = same and text_matches
        detail += "; PR text matches" if text_matches else "; pr_sha256 is missing or differs from pr.md"
    rep.add("coordinator review", rv == "ship" and same, detail,
            f"the coordinator reviews the current head and PR text, then records verdict: ship in {name}")


PRE_ISSUE_EXCLUDED = {"issue precondition evidence", "precedent precondition evidence", "issue closing keyword",
                      "issue assigned to author", "issue first decided", "human approval"}


def pre_issue(item):
    """Run the release checks that do not depend on opening or approving the issue first."""
    return _run(item, "release", exclude=PRE_ISSUE_EXCLUDED)


def run(item, mode=None, with_approval=False):
    mode = "release" if with_approval else mode or "review"
    if mode not in ("prepare", "review", "release"):
        raise ValueError("gate mode must be prepare, review or release")
    return _run(item, mode)


def _run(item, mode, exclude=()):
    rep = Report()
    git(item.clone, "fetch", "--quiet", "origin", item.meta.get("base", "main"), check=False)
    head = item.head_sha()
    core_checks(item, rep, head, mode=mode)
    if item.meta.get("line") == "oss":
        oss_checks(item, rep)
    try:
        checks.run_extra(item, rep, mode)
    except (OSError, ValueError) as error:
        rep.add("registered checks", False, str(error), "repair the evidence file and rerun the gate")
    if mode == "release" and item.meta.get("line") == "oss":
        coordinator_check(item, rep, head)
    if mode == "release" and "human approval" not in exclude:
        a = read_json(item.file("approval.json"))
        pr_hash = verdicts.pr_sha256(item)
        same = bool(a) and a.get("head_sha") == head and a.get("pr_sha256") == pr_hash
        rep.add("human approval", same,
                f"approved {a['head_sha'][:12]} at {a['at']}" + ("" if same else " (code or PR text changed since)") if a else "not approved",
                "complete the coordinator review of this head and PR text, then run bin/factory ship <item>, which records the owner's standing approval; bin/factory approve <item> --note '<owner quote>' is for a manual submit")
    rep.rows = [row for row in rep.rows if row["check"] not in exclude]
    result = {"at": iso(), "head_sha": head, "mode": mode, "ok": rep.ok, "rows": rep.rows}
    write_json(item.file("gate.json"), result)
    return rep, result
