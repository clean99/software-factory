# Value review

## Goal
Decide before code exists whether a maintainer would want the change. You did not propose it.

## Inputs
Item, source quotations, policy, proposal if present, and the live issue/discussion.

## Steps
1. For each of the five questions below, open the link to the original source and cite the evidence in the answer; a scout summary is not a source.
2. Is the source real? Check the external report or captured occurrence, its author and context.
3. Can users reach the input? Trace a supported user path, not just our fixture.
4. Does the contract support the claim? Compare the promised behavior with the documented sentence.
5. Is it not by design? Read history, documented semantics and workarounds.
6. Does the benefit exceed review and maintenance cost? Name the affected users and observable improvement.
7. Choose build only with five supported yes answers; choose propose when the behavior could be intended, and drop when only a probe of ours reaches it.

## Produce
<!-- schema2 -->
`value-review.json`: {reviewer, complete, answers:{source_is_real:{ok,note}, input_is_reachable_by_users:{ok,note}, contract_supports_it:{ok,note}, not_by_design:{ok,note}, benefit_exceeds_review_cost:{ok,note}}, verdict:build|propose|drop}.
Read demand.json's source/quote, contract, workaround, error_path and real_occurrence; do not rewrite the scout's evidence to make it pass.
<!-- /schema2 -->
<!-- legacy -->
Legacy items have no value-review record. Preserve their Markdown protocol; check Who hits it/Impact in verify.md against notes.md and the original source.
<!-- /legacy -->
For propose, write proposal.md: plan, API/UX sketch, scope, test plan and one concrete question, about ten lines.

## Check
<!-- schema2 -->
Stamp records demand_sha256 from demand.json bytes, not head or PR text. If demand changes, rerun the value review.
`bin/factory review stamp --item <item> value`
`bin/factory review check --item <item> value`
<!-- /schema2 -->
<!-- legacy -->
`bin/factory gate <item> --mode prepare`
<!-- /legacy -->

## Time budget
25 minutes. Save partial answers and return "complete": false if verification is unfinished; uncertainty about intended behavior means propose, not build.

## Never
Implement or publish a proposal. A self-filed issue alone is not demand; error-path changes need a real external report or captured occurrence, not an event type or injected fixture.
