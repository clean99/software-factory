"""Send one gate-ready, independently reviewed item: the coordinator's whole "send it" sequence.

Every step reuses the command it stands for (approve-proposal, open-issue, gate, approve, submit) and
stops the run with its reason. A step that cannot establish its precondition refuses; none is skipped.
"""
import argparse
import contextlib
import io
import pathlib
import re

from factory import cli
from factory.core import clean, clones, gate, ledger
from factory.core.slop import parse_pr_md
from factory.oss import disclosure, policy, submit
from factory.util import CONFIG, GH_READ_TIMEOUT, ROOT, CommandError, Item, gh_json, path_from_config, read_json, write_json

PLACEHOLDER = re.compile(r"PENDING_\w*|REPRO_URL|#ISSUE|\bTODO\b")
STOPPED = ledger.DONE | {"dropped", "parked", "blocked"}


class Stop(Exception):
    """The step refuses; the message says why."""


class Pause(Exception):
    """A dry run reached a step that needs an earlier write to happen first."""


def linked_worktree():
    """True when this checkout is a linked git worktree (clean.py's test): its runs/ and ledger are not the live ones."""
    return (ROOT / ".git").is_file()


def shown(path):
    path = pathlib.Path(path).resolve()
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def read(path):
    return path.read_text() if path.exists() else ""


def leftovers(path, allowed=()):
    """Placeholder tokens a draft still carries, other than those a later step fills in."""
    return sorted(set(PLACEHOLDER.findall(read(path))) - set(allowed))


def name_like(ref):
    return ref == pathlib.PurePath(ref).name and ref not in (".", "..") and not ref.startswith("~")


def locate(ref):
    """An item directory under runs/: a path to it (from the cwd or the factory root), or its exact name when ref has no path."""
    runs = path_from_config("runs_dir").resolve()
    if name_like(ref):
        found = sorted(p.parent.resolve() for p in runs.glob("*/*/item.json") if p.parent.name == ref)
    else:
        path = pathlib.Path(ref).expanduser()
        found = list(dict.fromkeys(p.resolve() for p in (path, ROOT / path) if (p / "item.json").is_file()))
    outside = [str(p) for p in found if not p.is_relative_to(runs)]
    if outside:
        raise Stop(f"{', '.join(outside)} is outside {runs}; ship sends only items of the factory checkout it runs from")
    if len(found) == 1:
        return found[0]
    if found:
        raise Stop(f"{ref} is ambiguous: {', '.join(map(shown, found))}")
    raise Stop(f"{ref} is neither a path to an item directory under {runs} nor the exact name of one")


def factory_checkout(path):
    """The factory checkout a path belongs to: the nearest directory, itself included, holding factory.config.json."""
    return next((p for p in (path, *path.parents) if (p / "factory.config.json").is_file()), None)


def repository_policy(item):
    """Read the gate's policy cache, refreshing a missing or outdated scan."""
    found = read_json(item.file("policy.json"))
    if not found or found.get("version", 1) < policy.VERSION:
        found = policy.run(item.meta["repo"])
        write_json(item.file("policy.json"), found)
    return found


def standing_note():
    note = (CONFIG.get("standing_approval_note") or "").strip()
    if not note:
        raise Stop("factory.config.json has no standing_approval_note; ship records approvals only under the owner's standing approval")
    return note


def quiet(command, **args):
    """Run a cli command for its effect; its printed line would break the JSON result."""
    with contextlib.redirect_stdout(io.StringIO()):
        command(argparse.Namespace(**args))


def failing(result):
    return "; ".join(f"{r['check']}: {r['detail']}" for r in result["rows"] if not r["ok"])


class Ship:
    def __init__(self, ref, dry_run):
        self.ref, self.dry_run, self.item, self.head = ref, dry_run, None, None
        self.out = {"ok": False, "item": str(ref), "dry_run": dry_run, "steps": [], "pr_url": None, "warnings": [], "notes": []}

    def run(self):
        steps = (("checkout", self.checkout), ("item", self.locate), ("unsubmitted", self.unsubmitted),
                 ("disclosure", self.disclosure), ("preconditions", self.preconditions), ("coordinator", self.reviewed), ("issue", self.open_issue),
                 ("placeholders", self.finished), ("gate", self.run_gate), ("approve", self.approve), ("submit", self.send),
                 ("verify", self.verify), ("clean", self.tidy))
        for name, step in steps:
            warned = len(self.out["warnings"])
            try:
                detail = step()
            except Pause as pause:
                self.out["steps"].append({"name": name, "ok": True, "detail": str(pause)})
                break
            except (Stop, CommandError, SystemExit, OSError, ValueError, KeyError) as error:
                detail = str(error) or type(error).__name__
                self.out["steps"].append({"name": name, "ok": False, "detail": detail})
                if name == "submit":
                    self.opened_anyway()
                elif self.out["pr_url"] and len(self.out["warnings"]) == warned:
                    self.out["warnings"].append(f"{name} failed after the PR was opened: {detail}")
                if name == "verify":
                    self.out["notes"].append("kept the clone's dependencies: the PR is unverified and may need another push")
                return self.out
            self.out["steps"].append({"name": name, "ok": True, "detail": detail})
        self.out["ok"] = True
        return self.out

    def opened_anyway(self):
        url = (read_json(self.item.meta_path) or {}).get("pr_url")
        if url:
            self.out["pr_url"] = url
            self.out["warnings"].append(f"submit stopped but item.json records {url}; check that PR and the ledger by hand")

    def checkout(self):
        if not linked_worktree():
            return f"main factory checkout {ROOT}"
        if not self.dry_run:
            raise Stop(f"{ROOT} is a linked worktree: ship writes runs/ and the ledger; run it from the main factory checkout")
        self.out["warnings"].append(f"dry run from the linked worktree {ROOT}: it read this worktree's runs/ and ledger, "
                                    "not the live ones in the main factory checkout")
        return "linked worktree: dry run only"

    def locate(self):
        self.item = Item(locate(self.ref))
        self.out["item"] = str(self.item.dir)
        clone = self.item.clone.resolve()
        home = factory_checkout(clone)
        if home and home != ROOT:
            raise Stop(f"its clone {clone} belongs to the factory checkout {home}, not {ROOT}; ship it from that checkout, "
                       "whose runs/ and ledger are the live ones")
        return shown(self.item.dir)

    def unsubmitted(self):
        meta = self.item.meta
        if meta.get("pr_url"):
            raise Stop(f"already submitted as {meta['pr_url']}; follow-ups are approved and pushed with bin/factory submit, not ship")
        if meta.get("line") != "oss":
            raise Stop(f"line is {meta.get('line')!r}; ship sends oss items only")
        if meta.get("status") in STOPPED:
            why = f" ({meta['blocked_reason']})" if meta.get("blocked_reason") else ""
            raise Stop(f"item is {meta['status']}{why}; ship sends live items only")
        clones.ensure(self.item)
        return f"no PR yet (status {meta.get('status')})"

    def disclosure(self):
        rules = repository_policy(self.item)
        _, body = parse_pr_md(read(self.item.file("pr.md")))
        self.out["pr_body"] = disclosure.new_pr_body(body, rules)
        return "factory disclosure prepared from repository policy; pr.md and review bindings unchanged"

    def preconditions(self):
        """submit's read-only refusals, before the issue opens: an issue whose PR cannot follow invites a duplicate (L-055)."""
        reason = submit.preconditions(self.item)
        if reason:
            raise Stop(reason + "; no issue opened and nothing approved")
        stacked = self.item.meta.get("stacked_on")
        return "no lesson owed; flood limits leave room" + (f"; pr.md names {stacked}" if stacked else "")

    def reviewed(self):
        rep, result = gate.pre_issue(self.item)
        self.noted(result)
        if not rep.ok:
            raise Stop(rep.text())
        return f"release checks before issue creation pass at {result['head_sha'][:12]}"

    def open_issue(self):
        meta = self.item.meta
        if meta.get("issue_first") != "issue":
            return f"issue_first={meta.get('issue_first')}: no issue to open"
        if meta.get("issue"):
            return f"issue {meta['issue']} already open"
        draft, pr = self.item.file("issue.md"), self.item.file("pr.md")
        if not draft.exists():
            raise Stop("issue_first is issue but issue.md is missing")
        tokens = leftovers(draft)
        if tokens:
            raise Stop("issue.md still has placeholders: " + ", ".join(tokens))
        if "#ISSUE" not in read(pr):
            raise Stop("pr.md has no #ISSUE for the new issue's number; add 'Fixes #ISSUE' so the PR links it (L-055)")
        tokens = leftovers(pr, allowed=("#ISSUE",))
        if tokens:
            raise Stop("pr.md still has placeholders besides #ISSUE: " + ", ".join(tokens)
                       + "; the PR must be able to go out with the issue (L-055)")
        note = standing_note()
        if self.dry_run:
            raise Pause("would approve issue.md under the standing note, open it, link pr.md and drop qualify.json; "
                        "the gate, approval and submit run once the issue exists")
        quiet(cli.cmd_approve_proposal, item=str(self.item.dir), file="issue.md", note=note)
        quiet(cli.cmd_open_issue, item=str(self.item.dir), file="issue.md", before_fix=False)
        self.item = Item(self.item.dir)
        url, number = self.item.meta.get("tracking_issue"), self.item.meta.get("issue")
        if not number or "#ISSUE" in read(pr):
            raise Stop(f"open-issue reported {url} but did not record the issue number in item.json and pr.md")
        self.item.file("qualify.json").unlink(missing_ok=True)
        return f"opened {url}; pr.md links #{number}; qualify.json dropped so the gate re-qualifies"

    def finished(self):
        """Whatever issue and issue_first say, the PR text is approved only once nothing in it is left to fill in."""
        tokens = leftovers(self.item.file("pr.md"))
        if tokens:
            raise Stop("pr.md still has placeholders: " + ", ".join(tokens) + "; fill them in before it is approved")
        return "pr.md has no placeholders"

    def noted(self, result):
        """WARN rows pass the gate; with no human reading the gate report before approval, they travel in the result."""
        warns = [r for r in result["rows"] if r.get("warn")]
        self.out["notes"] = [f"gate WARN {r['check']}: {r['detail']}" for r in warns]
        if any(r["check"] == "CLA" for r in warns):
            self.out["notes"].append(f"owner action: the owner signs the {self.item.meta['repo']} CLA/DCO on the PR; "
                                     "record it in docs/owner-actions.md")
        return warns

    def run_gate(self):
        _, result = gate.run(self.item)
        if not result["ok"]:
            raise Stop("gate fails (full report in gate.json): " + failing(result))
        self.head = result["head_sha"]
        warns = self.noted(result)
        checks = ", ".join(dict.fromkeys(r["check"] for r in warns))
        return f"PASS at {self.head[:12]}" + (f" with {len(warns)} WARN row(s) ({checks}), listed in notes" if warns else "")

    def unmoved(self, head):
        if head != self.head:
            raise Stop(f"the branch moved after the gate ({self.head[:12]} -> {str(head)[:12]}); run ship again")

    def approve(self):
        note = standing_note()
        self.unmoved(self.item.head_sha())
        if self.dry_run:
            return f"would approve {self.head[:12]}: {note}"
        quiet(cli.cmd_approve, item=str(self.item.dir), note=note)
        self.item = Item(self.item.dir)
        self.unmoved((read_json(self.item.file("approval.json")) or {}).get("head_sha"))
        return f"approved {self.head[:12]}: {note}"

    def send(self):
        if self.dry_run:
            return "would submit: fork, push and open the PR"
        result = submit.run(self.item)
        if not result.get("ok"):
            raise Stop("; ".join(str(x) for x in (result.get("reason"), *result.get("findings", []), result.get("report")) if x))
        self.out.update(ok=True, pr_url=result["url"])
        return "; ".join(filter(None, (result["url"], result.get("note"))))

    def verify(self):
        if self.dry_run:
            return "would compare the PR head and commit authors with the item"
        email, warnings = CONFIG["git_email"], self.out["warnings"]
        try:
            head = self.item.head_sha()
            pr = gh_json("pr", "view", self.out["pr_url"], "--json", "headRefOid,commits", timeout=GH_READ_TIMEOUT) or {}
        except (CommandError, ValueError) as error:
            warnings.append(f"could not read the PR back or the item head; the PR head and authors are unverified: {error}")
            raise Stop(warnings[-1]) from None
        if pr and pr.get("headRefOid") != head:
            warnings.append(f"PR head is {str(pr.get('headRefOid'))[:12]}, not the item head {head[:12]}: the push did not reach the PR")
        commits = pr.get("commits") or []
        if pr and not commits:
            warnings.append("the PR lists no commits; authors are unverified")
        for commit in commits:
            emails = [a.get("email") for a in commit.get("authors") or []]
            if not emails or any(e != email for e in emails):
                warnings.append(f"commit {str(commit.get('oid'))[:9]} authored by {', '.join(map(str, emails)) or 'nobody'}, not {email}")
        if not pr and not warnings:
            warnings.append("gh pr view returned nothing; the PR head and authors are unverified")
        if warnings:
            raise Stop("; ".join(warnings))
        return f"PR head is {head[:12]}; {len(commits)} commit(s) authored as {email}"

    def tidy(self):
        """Free the clone as bin/factory submit does, once the PR is verified; the PR is open, so a failure only warns."""
        if self.dry_run:
            return "would drop the clone's dependencies unless another item is still being built in it"
        try:
            return "dependencies dropped: " + clean.after_submit(self.item)
        except (Exception, SystemExit) as error:
            raise Stop(f"{type(error).__name__}: {error}; the clone keeps its dependencies") from None


def run(ref, dry_run=False):
    return Ship(ref, dry_run).run()
