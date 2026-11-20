# Fix review findings

## Goal
Resolve one review round without introducing a new contract or hiding a regression.

## Inputs
Current item/head, review, archived rounds, original demand and tests. The fix round number is supplied from disk. If fix_rounds is already 1 or more, stop and request takeover; do not start another automatic correction round.

## Steps
1. Read the whole current review and prior rounds. An unfinished review (complete=false or no verdict) goes to coordinator takeover; do not archive it or consume a fix round.
2. Verify each finding against the source and receipt rather than blindly applying its suggested patch.
3. Archive review.json or review.md as the next unused review-round<N>.json or .md before editing.
4. Reproduce every accepted blocking finding through `bin/factory run --item <item> --cwd <clone> --expect-fail -- <command>`.
5. Apply a focused fix on the same item branch.
6. Add regression coverage for accepted findings.
7. Update required changesets and repository docs.
8. Run owning CI lint, format, typecheck, tests and build through factory run.
9. Commit the verified fix as a new commit.
10. Rerun required_checks through factory run at the committed head.
11. Run `bin/factory redgreen <item>`.
12. Update validation.md, verify.md and pr.md.
13. Run `bin/factory slop <item>`.

## Produce
`validation.md`: finding → accepted/rejected with evidence → test/receipt → outcome; `verify.md`: fresh self-review with true plain `Who hits it:` and `Impact:` lines and exactly one current `verdict: ship|fix-needed|drop`; `pr.md`; `diff.patch`; updated test_cmd/test_cwd/test_files in item.json. Move the old verdict into historical prose, never append a second parseable verdict.
<!-- schema2 -->
Preserve `demand.json`, `value-review.json` and `required_checks:[{label,argv,cwd}]`; refresh valid current-head receipts for all required checks. Archive `review.json` to `review-round<N>.json`; leave the new independent review to the next reviewer.
<!-- /schema2 -->
<!-- legacy -->
Archive `review.md` to `review-round<N>.md`. Preserve legacy validation.md and redgreen.json references; do not substitute verify.json or change schema.
<!-- /legacy -->

## Check
`bin/factory gate <item> --mode prepare`

## Time budget
60 minutes, one correction round. Save partial results before expiry. If a correct fix needs a redesign beyond the findings, stop at the unchanged clean head and explain; the coordinator takes over or parks it. A second fix-needed review does not reset the budget on restart.

## Never
Write your own independent review or erase a previous review. Rewrite pushed commits, expand scope just to satisfy a bad finding, or set an already submitted item back to preparing.
