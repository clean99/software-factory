"""Commits already on the PR branch are public. Repo automation (autofix.ci, pre-commit.ci) and maintainers push
their own commits there, so their author and committer need not be our configured identity, and their addresses
are not ours to keep private (L-063). The same holds for the addresses in their Co-authored-by and Signed-off-by
trailers. Only those checks are waived: identity lines and trailers are still scanned for local identity markers and
private terms, which catch our own accounts, and the rest of the message and the content are scanned in full."""
import os
import re

from factory.util import GH_READ_TIMEOUT, CommandError, git, run

PUBLIC_ADDRESS = "email address"
TRAILER = re.compile(r"(?:Co-authored-by|Signed-off-by):\s", re.I)


def pr_head(item):
    """The fork branch behind the item's PR, fetched now; None when there is no PR or it cannot be read."""
    branch = item.meta.get("branch")
    if not item.meta.get("pr_url") or not branch:
        return None
    ref = f"refs/remotes/fork/{branch}"
    try:
        run(["git", "-C", str(item.clone), "fetch", "--quiet", "--no-tags", "fork", f"+refs/heads/{branch}:{ref}"],
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=GH_READ_TIMEOUT)
        return git(item.clone, "rev-parse", "--verify", f"{ref}^{{commit}}")
    except CommandError:
        return None


def on_pr_branch(item, base):
    """Commits past `base` the PR branch already carries; empty, so every commit is checked, when it cannot be read."""
    head = pr_head(item)
    if not head:
        return frozenset()
    try:
        return frozenset(git(item.clone, "rev-list", head, f"^{base}").splitlines())
    except CommandError:
        return frozenset()


def identity_waiver(lines):
    """privacy.findings waivers for the generic e-mail category on the given identity line numbers."""
    return frozenset((line, PUBLIC_ADDRESS) for line in lines)


def header_identity_lines(raw):
    """Line numbers of the author and committer headers of a raw commit object (never the message)."""
    header = raw.partition("\n\n")[0].split("\n")
    return [n for n, line in enumerate(header, 1) if line.startswith(("author ", "committer "))]


def trailer_lines(message, first=1):
    """Line numbers of Co-authored-by and Signed-off-by trailers in a commit message whose first line is `first`."""
    return [n for n, line in enumerate(message.split("\n"), first) if TRAILER.match(line)]


def raw_identity_lines(raw):
    """Author and committer headers and the identity trailers of a raw commit object."""
    header, _, message = raw.partition("\n\n")
    return header_identity_lines(raw) + trailer_lines(message, header.count("\n") + 3)
