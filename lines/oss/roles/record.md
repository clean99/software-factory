# Record completed work

## Goal
Prepare accurate records of contribution events without inventing benefits or treating generated files as published records.

## Inputs
Ledger row, live PR outcome/head, maintainer feedback, item evidence and `bin/factory sync status`.

## Steps
1. Re-read the live PR before describing its outcome.
2. Read the ledger row and missing destination receipts.
3. Write a bounded user-facing phrase describing only the accepted change.
4. Identify whether this event teaches a new technical insight, not just a status change.
5. Prepare the minimal interview-guide/question/handoff additions for that insight.
6. Record any rejection or generalizable review convention with source, scope and a proposed enforceable rule.
7. Hand the exact local paths and missing profile/teach/notebook destinations to the coordinator.

## Produce
`record.json` in the item: {url, observed_state, phrase, destinations:[{name:profile|teach|notebook, proposed_file, remaining}], insight, lesson:{source, scope, rule, enforcement}, owner_actions}.
Teaching text in the item's `interview.md`: problem/users, root cause, design trade-off, proof, benefit and current state in Chinese. Link existing repo material rather than rewriting it; an event without a new insight needs only generated status/table updates.
<!-- schema2 -->
Use receipt IDs for execution claims and preserve provenance.json. After a real write and read-back, the coordinator runs `bin/factory sync mark <destination> [<pr-url>] --file <read-back file or directory> [--ref <label>]`. The CLI checks PR mentions and captures {at,ref,sha256} from the matching file, not a typed digest; a directory includes .md and .txt files. Omitting the URL captures mentioned merged PRs still owed and lists misses. Private notebook can instead use `<pr-url> --deferred '<reason>'`; deferred or empty-digest receipts remain owed.
<!-- /schema2 -->
<!-- legacy -->
Read legacy validation.md and review.md (plain reviewer/head/verdict fields) without rewriting history. A legacy recorded_at alone is not proof each destination is current.
<!-- /legacy -->

## Check
`bin/factory sync status`

## Time budget
30 minutes. Save partial drafts and the exact owed destinations before expiry; do not block urgent feedback on teaching prose.

## Never
Count closed-but-adopted work as merged. Hand-copy derived counts, rewrite all old teaching sections, mark a destination complete on file existence alone, or turn a single observation into an established global rule. Proposed lessons start candidate; confirmation, retirement and enforcement belong in the routed lesson data.
