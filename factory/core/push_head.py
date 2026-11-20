"""`factory push --item` on an open PR's branch records what it pushed as the item's `pushed_head`, so
`reply` and `thread-reply` compare the live PR head with our own last push instead of the head `submit`
recorded. The commit is resolved before pushing and recorded only when the remote branch did not hold it
before the push, the push exited 0 and the branch shows exactly that commit afterwards; anything less leaves
item.json alone and says why."""
import sys

from factory.core import publication
from factory.util import GH_READ_TIMEOUT, CommandError, Item, git, iso, read_json, run

VALUE_OPTIONS = {"--repo", "--receive-pack", "--exec", "-o", "--push-option"}
NOTHING_PUSHED = ("--dry-run", "--delete")


def operands(args):
    """Positional git push operands, or None for a dry run or a deletion."""
    words, repo, i = [], None, 0
    while i < len(args):
        arg = args[i]
        name, _, value = arg.partition("=")
        if arg == "--":
            words += args[i + 1:]
            break
        if arg in VALUE_OPTIONS:
            i += 1
            if arg == "--repo" and i < len(args):
                repo = args[i]
        elif name == "--repo":
            repo = value
        elif arg.startswith("--"):
            if any(option.startswith(name) for option in NOTHING_PUSHED):
                return None
        elif arg.startswith("-"):
            if {"n", "d"} & set(arg[1:]):
                return None
        else:
            words.append(arg)
        i += 1
    return words or ([repo] if repo else [])


def destination(refspec, cwd):
    """The source of one refspec and the branch it updates (None unless it names one branch)."""
    source, colon, dest = refspec.removeprefix("+").partition(":")
    if not colon:
        dest = git(cwd, "symbolic-ref", "--quiet", "HEAD", check=False) if source == "HEAD" else source
    if not source or source.startswith("^") or "*" in refspec:
        return source, None
    if dest.startswith("refs/"):
        return source, dest.removeprefix("refs/heads/") if dest.startswith("refs/heads/") else None
    return source, dest


def plan(args, cwd, meta):
    """((remote, branch, sha), None) when the push updates the item's PR branch, else (None, reason) where
    reason is None when there is nothing to record."""
    if not meta.get("pr_url") or not meta.get("branch"):
        return None, None
    words = operands(args)
    if words is None:
        return None, None
    if len(words) < 2:
        return None, f"name the remote and the PR branch in the push, e.g. fork {meta['branch']}"
    for refspec in words[1:]:
        source, branch = destination(refspec, cwd)
        if branch == meta["branch"]:
            sha = git(cwd, "rev-parse", "--verify", "--quiet", "--end-of-options", source + "^{commit}", check=False)
            return ((words[0], branch, sha), None) if sha else (None, f"{source} did not resolve to a commit before the push")
    return None, None


def remote_heads(cwd, remote, branch):
    """Commits `git ls-remote` shows on the branch, or None when the remote cannot be read."""
    ref = "refs/heads/" + branch
    try:
        out = run(["git", "-C", str(cwd), "ls-remote", "--", remote, ref], timeout=GH_READ_TIMEOUT).stdout
    except CommandError:
        return None
    return [line.split("\t", 1)[0] for line in out.splitlines() if line.endswith("\t" + ref)]


def unpushed(cwd, meta, target):
    """(target, reason) after reading the branch before the push. A branch that already holds the commit
    records nothing: an up-to-date push does not show that we pushed it (it may be a maintainer's commit
    we fetched), and only an item that already expects that head stays quiet."""
    remote, branch, sha = target
    heads = remote_heads(cwd, remote, branch)
    if heads is None:
        return None, f"git ls-remote {remote} refs/heads/{branch} failed before the push"
    if heads != [sha]:
        return target, None
    if sha == (meta.get("pushed_head") or meta.get("head_sha")):
        return None, None
    return None, f"{remote} refs/heads/{branch} already held {sha} before the push, so this push did not put it there"


def confirm(cwd, remote, branch, sha):
    """Why the remote branch does not show `sha` after the push, or None when it does."""
    heads = remote_heads(cwd, remote, branch)
    if heads is None:
        return f"git ls-remote {remote} refs/heads/{branch} failed after the push"
    if heads != [sha]:
        return f"{remote} refs/heads/{branch} is at {', '.join(heads) or 'no commit'} after the push, not the pushed {sha}"
    return None


def push(args, cwd, item_dir):
    try:
        meta = read_json(item_dir / "item.json")
        target, reason = plan(args, cwd, meta)
    except (OSError, ValueError):
        target, reason = None, "item.json could not be read before the push"
    if target:
        target, reason = unpushed(cwd, meta, target)
    code = publication.run("push", args, cwd, item_dir)
    if code:
        return code
    if target:
        reason = confirm(cwd, *target)
    if reason:
        print(f"warning: pushed_head not recorded: {reason}. Re-read the PR head before reply or thread-reply", file=sys.stderr)
    elif target:
        item = Item(item_dir)
        item.meta.update(pushed_head=target[2], pushed_at=iso())
        item.save()
        print(f"recorded pushed_head {target[2]} in {item.dir.name}/item.json")
    return code
