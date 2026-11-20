# Scout

## Goal
Select one wanted, unclaimed change; zero candidates is a valid result. Do not implement it.

## Inputs
Task scope, repo, current policy and health, existing items and PRs.

## Steps
1. Run `bin/factory health <repo>`.
2. Run `bin/factory policy <repo>`.
3. Read maintainer follow-ups, endorsed issues and unchecked tracking items in that order.
4. Read every comment and closed attempt for the best candidate.
5. Check claims, assignees, local items, our PRs, open/merged/closed and fork PRs by issue, exact phrase, symbol and file (inspect at least the last 30 open PR file lists).
6. Run `bin/factory qualify <target>`.
   For a new candidate, target is owner/repo#N or owner/repo without an issue; include `--keyword "<phrase>"` for each collision phrase and `--discussion` for a discussion in both qualify and new.
7. Create at most one eligible item with `bin/factory new <target> --slug <slug> --kind <small|large> --implementer <identity>` only if no item already exists.
8. Record the real usage, documented contract, correct sibling and dated maintainer signal before any code changes.
9. Draft `proposal.md` instead of a full implementation when scope or invitation is missing.

## Produce
`notes.md` in the item: examined candidates, selected/rejected reasons, source URLs/dates, grades, sibling and remaining scope. With zero candidates return the reasons in summary; do not create a report outside an item.
<!-- schema2 -->
`demand.json`: {claim, who_hits_it:{kind:external-report|maintainer-request|live-capture|documented-contract, source, quote, self_filed}, contract:{says, source}, workaround, error_path, real_occurrence, decision:build|propose|drop}. Use booleans for self_filed/error_path and a source URL or item-relative evidence path for real_occurrence.
<!-- /schema2 -->
<!-- legacy -->
`verify.md`: plain `Who hits it: <source and supported usage>` and `Impact: <visible change>` lines. Keep the legacy item schema. Put the contract, workaround and occurrence evidence in `notes.md`.
<!-- /legacy -->
For conversion sweeps add `manifest.json`: {scope, matrix:[{type, field, direction, status:preserved|lost|no-slot, evidence}], rejected}. Keep probes in an owned base worktree and remove it afterwards.

## Check
<!-- schema2 -->
`bin/factory review check --item <item> demand`
<!-- /schema2 -->
<!-- legacy -->
`bin/factory qualify <target>`
<!-- /legacy -->

## Time budget
45 minutes. Write partial evidence and blockers before time expires; return build, propose or drop, never invent a candidate to fill a slot.

## Never
Continue after qualify says skip, or create a second item on retry. Treat a self-filed issue, sibling merge, hypothetical probe or error-event enum as proof that users hit this change. Reject bugs younger than 48 hours unless a maintainer invited a PR. A broad invitation is not agreement to a particular API/UX design. Keep paid-key/GPU-only verification out of a ready-to-build choice.
