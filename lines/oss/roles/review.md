# Independent review

## Goal
You did not write this change. Try to break it and decide whether a maintainer should merge it now.

## Inputs
Item, live threads, diff, tests, demand, validation, verify.md, pr.md, issue/reply drafts and prior rounds. Confirm the branch equals the supplied head; do not switch the shared clone.

## Steps
1. Read the full diff from the merge base and surrounding code for every hunk.
2. Check Who hits it/Impact against original sources.
3. Read `bin/factory review classes`. For each applicable class, first write the list it asks for: every writer and when it runs; every reader of the reused mode, flag or shape; every sibling; or the class's equivalent inventory. Server acceptance, accumulated/streamed input and contract ownership are classes too; where relevant also cover null/empty, concurrency, i18n, accessibility, SSR and platforms.
4. Choose the input that would break the weakest entry, then run it at base and head; do not substitute a familiar probe for the inventory.
5. Create your merge-base worktree outside the factory checkout (`git -C <clone> worktree add --detach <dir> <merge-base-sha>`); install dependencies there. Reproduce with `bin/factory run --item <item> --cwd <dir> --expect-fail -- <command>`.
6. Run the same proof at head with `bin/factory run --item <item> --cwd <clone> -- <command>`; bind head receipts to each class answer.
7. Match required_checks to CI commands/toolchain matrix. Include redgreen.json's exact command/cwd unless no_test_reason applies; receipts do not prove CI completeness.
8. Check pr.md against the template/disclosure rule, reproduction, issue/reply accuracy and checkboxes.
9. Re-run `bin/factory qualify <target>` for collisions.
10. Distinguish regressions/spec misses from pre-existing gaps not worsened here. Preserve proof inputs under the item; remove only your base worktree.

## Produce
<!-- schema2 -->
`review.json`: {reviewer, complete, obligations:[{class, applies, why, receipts:[id]}], findings:[{severity:blocking|should-fix|nit|pre-existing, where, summary, receipt}], verdict:ship|fix-needed|drop}.
Answer every class; keep inventories in why or an item-local file cited there. Applying classes need valid head receipts; blocking findings need reproducing receipts. The stamp command supplies head and pr_sha256, never calculate or type them. Use `bin/factory review render --item <item>` for Markdown.
<!-- /schema2 -->
<!-- legacy -->
`review.md` starts with `reviewer: <identity> (fresh context)`; the stamp command adds the head. Include class inventories, file:line, input, receipts, requested change and regression/pre-existing classification; end with one plain `verdict: ship|fix-needed|drop`. Verify Who hits it/Impact in verify.md. Keep this Markdown protocol, not replacement JSON.
<!-- /legacy -->
Use actual reviewer identity; large legacy items require a different identity, not just a fresh suffix.

## Check
`bin/factory review stamp --item <item> review --head <head>`
`bin/factory review check --item <item> review`
<!-- legacy -->
Also run `bin/factory gate <item> --mode review`.
<!-- /legacy -->

## Time budget
120 minutes unless review_budget_minutes says otherwise. Save partial findings and return "complete": false when unfinished; do not turn incomplete verification into fix-needed. Complete ship requires no unresolved blocking/should-fix findings; drop unsupported demand.

## Never
Edit production code, count probes as coverage or invent style rules.
