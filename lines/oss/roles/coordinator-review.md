# Coordinator review

## Goal
Personally verify this exact head and PR text before any release path. An independent review is input, not a substitute for judgment.

## Inputs
Full diff, tests, pr.md, demand.json (legacy: Who hits it/Impact), independent review, redgreen.json, receipts, issue/reply drafts and live PR state.
For work you do yourself, provenance is your original session's explicit role/item task and actual assistant model, not a worker name or check summary. See docs/repairs.md#execution-provenance; receipts prove checks, not identity or independence.

## Steps
1. Read the diff from the merge base end to end before any rerun, including surrounding paths.
2. Read all changed tests and pr.md against the repository template and disclosure rule.
3. Read demand and original sources to decide whether the change is wanted.
4. Reproduce the key defect on base and its repair at head through factory run.
5. Rerun the redgreen.json command at its recorded cwd with `bin/factory rerun --item <item> <receipt-id>`; on schema 2 it must be in required_checks when red→green applies.
6. Rerun at least one receipt per applying review obligation through the same command.
7. Choose an input the reviewer did not try, from the class marked not applying with the weakest reason; run that counterexample through factory run. If every class applies, challenge the weakest applying answer instead and explain why.
8. Apply justified PR/reply wording nits before stamping the final review.
9. Choose ship only after inspecting outputs and unresolved findings.

## Produce
<!-- schema2 -->
`coordinator-review.json`: {complete, read:{diff,tests,pr_text,demand}, reruns:[receipt-id], counterexample:{what,receipt}, verdict:ship|fix-needed|drop}.
The stamp command supplies head and pr_sha256; never calculate or type them. `reruns` accepts receipts made at the current head only, and each rerun's original must also be head-bound. Keep a base (red) receipt in `receipts/` and cite it in the existing `validation.md`; do not put it in `reruns` or rewrite its commit fields. Reruns need CLI-computed match.exit=true and must cover redgreen and every applying obligation. The counterexample argv must differ from every reviewer-cited receipt. Recreate any removed base worktree at its recorded commit before rerunning; missing proof is not a pass.
<!-- /schema2 -->
<!-- legacy -->
`coordinator-review.md`: plain reviewer identity, findings with file:line, base/head receipts, reruns and your counterexample, then one plain `verdict: ship|fix-needed|drop`; stamp adds the head. Check Who hits it/Impact in verify.md. Capture legacy redgreen's command with factory run before rerun if no receipt exists.
<!-- /legacy -->

## Check
`bin/factory review stamp --item <item> coordinator --head <head>`
`bin/factory review check --item <item> coordinator`
<!-- legacy -->
Also run `bin/factory gate <item> --mode release`.
<!-- /legacy -->

## Time budget
60 minutes. Save partial findings and return "complete": false if unfinished. Fix-needed after the correction round or over-budget review means takeover or park, not another automatic loop.

## Never
Copy the review verdict, type receipt exit/match values, sample only easy proofs, or treat gate PASS as approval. Code or PR-text changes invalidate the corresponding review binding. This card grants no release authority.
