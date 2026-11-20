# Implement

## Goal
Build the smallest complete fix in the repository's style, with a source-only red→green proof.

## Inputs
Item, accepted demand/value decision, issue comments and closed attempts, policy flags, current branch.

## Steps
1. Read qualification warnings and the full issue discussion.
2. Re-run `bin/factory qualify <target>` before coding.
3. Confirm exclusive use of the clone.
4. Check out the item's branch (new creates it but does not switch it).
5. Read the whole affected path and its three closest neighbors.
6. Reproduce before changing anything, while the branch equals base: `bin/factory run --item <item> --cwd <clone> --expect-fail -- <command>`.
7. Write `design.md` before a large change: problem, approach, rejected alternatives, API/UX, files, tests, risks.
8. Implement the minimal root-cause change in neighboring style.
9. Add one focused behavioral regression in the neighbors' test style.
10. Update stale docs/comments, public examples and required changesets.
11. Iterate with the focused test and lint until they pass; these runs are not evidence.
12. Set test_cmd/test_cwd/test_files in item.json; item.json required_checks lists the repository's CI commands and must include the exact regression command recorded by bin/factory redgreen.
13. Commit the verified change in the repository's message style.
14. Run every required check (owning CI lint, format, typecheck, full package tests, build) at the committed head through `bin/factory run --item <item> --cwd <clone> -- <command>`; a fix means a new commit and a fresh run.
15. Run `bin/factory redgreen <item>`.
16. Write the PR draft, validation, interview material and adversarial self-review.
17. Run `bin/factory slop <item>`; any fix requires a new commit and fresh proofs before handoff.

## Produce
`item.json`: test_cmd, test_cwd, test_files; `diff.patch`; `validation.md`: reproduction, CI commands with receipt links, redgreen.json reference, demo or infeasibility reason, collision check; `pr.md`: `# <title>` then the exact repository template; `interview.md` in Chinese: problem/users, root cause, change, proof, engineering lesson, benefit, live status.
`verify.md`: plain `Who hits it: ...`, `Impact: ...`, rejection reasons/findings and one final `verdict: ship|fix-needed|drop`. Audit scope, style, comments, edges, test strength and PR claims; fix findings before ship.
<!-- schema2 -->
Require `demand.json` and an independent `value-review.json` verdict build before implementation. Set `required_checks:[{label,argv,cwd}]` from the owning CI; cite signed factory run receipt IDs, never write receipts. At least one required check runs tests.
<!-- /schema2 -->
<!-- legacy -->
Keep `validation.md`, `verify.md` and `redgreen.json` as the gate expects; preserve the plain impact/verdict lines. Do not migrate the item's schema.
<!-- /legacy -->
For large work add `design.md`; for visible work `demo/`; for issue-first `issue.md` with runnable reproduction and `resolves #ISSUE` in pr.md.

## Check
`bin/factory gate <item> --mode prepare`

## Time budget
90 minutes. Run bounded foreground commands with explicit timeouts; write partial files and remaining gate failures before time expires. If reproduction fails, record blocked_reason and stop.

## Never
Write the independent review. Add drive-by edits, trivial comments or padding to hit a line count. Change status/files after handoff. Call a package test a full-workspace pass. Build an unaccepted public API/UX design.
