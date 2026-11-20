# Take over an unfinished item

## Goal
Recover an over-budget review or failed correction without repeating the same blind spot. Narrow, finish or park it.

## Inputs
Item, timeline, all review-round files, last review, demand/value decision, current local and remote heads, partial validation and receipts.

## Steps
1. Read the original demand and current live issue/PR state.
2. Read the entire diff and every prior finding with its reproduction.
3. Identify the missed contract, readers and writers rather than accepting the last proposed patch.
4. Write the repair decision or park reason with a reopening condition in takeover.json, then record it with `bin/factory review takeover --item <item> --head <head>`. Repeating an unchanged decision does not restart the review budget.
5. Preserve prior review files under the next unused review-round<N> name before repair.
6. Implement the bounded repair on the existing branch.
7. Add regression coverage for the missed contract.
8. Update required changesets and repository docs.
9. Run owning CI checks through factory run.
10. Update test metadata.
11. Commit the verified repair as a new commit.
12. Rerun required_checks through factory run at the committed head.
13. Run `bin/factory redgreen <item>`.
14. Run `bin/factory slop <item>`.
15. Update the PR draft, validation, self-review and remaining gate failures for a fresh independent review.

## Produce
`takeover.json`: {decision:repair|park, reason, reopen_when, findings:[{finding, disposition, receipt}], remaining}; updated `validation.md`, `pr.md`, `diff.patch`, test_cmd/test_cwd/test_files and `verify.md` with one current verdict and true Who hits it/Impact lines.
<!-- schema2 -->
Preserve demand.json/value-review.json and all signed receipts; update required_checks to the actual touched CI scope. Retain review-round<N>.json; do not write the new review.json yourself.
<!-- /schema2 -->
<!-- legacy -->
Preserve review-round<N>.md and redgreen.json. Keep plain `Who hits it:`, `Impact:`, `verdict: ship|fix-needed|drop` lines in verify.md; require a new independent review.md with plain reviewer/head/verdict lines.
<!-- /legacy -->

## Check
`bin/factory gate <item> --mode prepare`

## Time budget
90 minutes. Save partial work before expiry. If repair is not bounded, park with a reason; do not leave a background job pretending the item is complete.

## Never
Reset fix_rounds, discard failed proofs, revive a dropped item without new evidence, or bypass independent and coordinator reviews because you took over.
