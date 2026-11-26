# Follow maintainer feedback

## Goal
Address current feedback on an open PR and prepare an accurate reply to the right thread.

## Inputs
Item, PR URL, remote head, live review threads (community too), failing job logs, maintainer decisions.

## Steps
1. Re-read PR state/head; stop on obsolete events or a non-open PR.
2. Map each request to its inline thread's first comment id or conversation/review destination before any push.
3. Check whether a maintainer already settled each bot request on an earlier PR.
4. Read each failing CI job log; compare the same job/version/error on base before classifying it.
5. Reproduce accepted code findings with factory run.
6. Archive the prior review to the next review-round<N> file before changing code.
7. Fix accepted requests on the same branch. For conflicts, merge the PR base into the contribution branch. Rebase only when the repository's contribution rules require it: cite the rule and save the remote SHA before rebasing. After fresh reviews and approval, only the coordinator may publish: `bin/factory push -C <checkout> --item <item> -- --force-with-lease=refs/heads/<branch>:<observed-sha> fork HEAD:refs/heads/<branch>`. If the remote head changed, stop; never use bare --force or an implicit lease.
8. Add a regression test for the request.
9. Update required changesets and repository docs.
10. Run owning CI checks through factory run.
11. Commit the verified fix as a new commit.
12. Rerun required_checks through factory run at the committed head.
13. Run `bin/factory redgreen <item>`.
14. Update validation.md, verify.md and pr.md.
15. Run `bin/factory slop <item>`.
16. Draft a one-to-three-sentence reply answering the question, without process notes, argument or over-apology.

## Produce
`reply.md` (or next reply-N.md); `follow.json`: {reply_file, reply_to, destination:inline|conversation or per-thread replies:[{file,reply_to,destination}], reply_only, observed_head, requests:[{request, disposition, evidence}], ci:[{job, run, head, version, classification, evidence}]}.
Keep item.json `submitted`; never overwrite a newer coordinator state. Reply-only: reply_only=true, keep the head, still request text review.
If blocked, save `head`, `event_keys`, evidence-backed `blockers` and `reopen_when` in follow.json. Refresh alone does not retry. Clear blockers with resolving evidence; new feedback/head permits reassessment, not publication.
<!-- schema2 -->
Keep demand/value decisions and required_checks intact; refresh current-head receipts. Archive review.json as review-round<N>.json; the next reviewer writes review.json.
<!-- /schema2 -->
<!-- legacy -->
Archive review.md as review-round<N>.md. Keep validation.md and verify.md; use plain `Who hits it:`, `Impact:` and one current `verdict: ship|fix-needed|drop` in verify.md. The next reviewer supplies plain reviewer/head/verdict lines in review.md.
<!-- /legacy -->

## Check
`bin/factory gate <item> --mode prepare`

## Time budget
60 minutes, one round. Write partial findings before expiry; hand redesigns or repeated failures to the coordinator. Reply-only is preparation, not permission to post.

## Never
Treat a known job name as a CI waiver: external/base failures need current evidence and must not hide new head failures. Preserve CLA as a separate blocker. Merge main or bump a silent PR solely to make checks green. Overwrite someone else's new remote commit or confuse a review ID with an inline comment ID. Skip review because only text changed.
