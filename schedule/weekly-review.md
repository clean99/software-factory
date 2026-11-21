# Weekly review

Run this procedure under the same factory shift lease as the daily coordinator, using the same coordinator and sub-agent routes. The Sunday slot is 22:30 Asia/Singapore; `schedule.json` holds exact model IDs, the three-hour limit, and the eight-hour grace window. The next deployment is on the Devbox, after separate migration verification and owner release confirmation; do not enable a new loop on the Mac. The scheduler waits for any daily runner and lease. It never preempts them.

## Start and define the period

1. Read `AGENTS.md`, `docs/direction.md`, `docs/metrics.md`, the previous weekly retro, and the last hypothesis. Use the run key supplied by the scheduler; use its slot date for `docs/retro/<date>-weekly-retro.md`, even when a late run crosses midnight.
2. Run `bin/factory shift start --owner <session-name> --ttl-hours 3`. Stop on a refused lease. For an already-open session, continue it rather than starting another. Keep its generation-bound renewal current while workers run.
3. Complete any saved-main push printed at start, then refresh upstream observations once with `bin/factory next --refresh`, as described in `session-shift.md`. Read the resulting evidence; this review is not an instruction to run the daily contribution queue or ship a new item.
4. While holding the lease, check the dated retro files. If this period already has a completed weekly review or Monday fallback page, report its path and end without writing another. A Sunday page dated within six days suppresses Monday's “Measure the week” action in `factory/core/nextaction.py`. If Sunday was skipped, that action remains the fallback.

## Read the evidence

Use a seven-day event window ending at this slot, with explicit Asia/Singapore boundaries and source timestamps. Separate populations rather than mixing them:

- New submissions by `submitted_at`; merge and closure events by their actual event times, including events on older submissions. Do not label a submitted-since cohort as all of this week's merges.
- Closure reasons, including adopted elsewhere, collisions, lack of demand, and unanswered work. Preserve uncertainty when evidence is absent; do not infer rejection from silence.
- Rework after publication, gate refusals before publication, independent-review send-backs, and coordinator corrections. Separate upstream-requested changes from defects the factory introduced.
- One or two concrete cases with item paths, source links, review findings, and test or command receipts. Read the underlying outputs rather than relying on a rate alone.
- Last week's hypothesis: its baseline, intervention, expected number, current comparable number, and whether it moved. Separate a delayed signal from a failed intervention.

Use `bin/factory sync retro --since <window-start> --write workspace/retro/<date>` for source material and refresh the supported metrics snapshot. Read `docs/metrics.md` before comparing cohorts. The historical `2026-11-09-weekly-retro.md` is useful as a case record, not a template for volume targets, response-time targets, or current metric denominators. Do not time or pressure maintainers. Do not rotate a lane on less than a comparable two-week observation window.

## Choose and test improvements

Choose one or two substantive improvements that address the evidence. State what they change, which failure they prevent, and which number should move. Avoid cosmetic churn, padding, and speculative cleanup.

Commit data refreshes and `docs/retro/<date>-weekly-retro.md` as factory records on `main`, using the normal `bin/factory commit -m '<what changed>' --path <path>` workflow after its checks. The retro records findings, proposed improvements, and next week's hypothesis; it must not describe a pending code change as deployed.

Changes to production code, policy, role cards, public templates, or public copy require owner review:

1. Prepare them on `weekly/<run key>`, never as a record commit on main. Keep record-only commits separate from improvement changes. Delegate bounded implementation and tests to sub-agents; give each worker owned paths and pass evidence by path. Keep the existing concurrency limit.
2. Run required lint and unit tests, plus the factory suite when factory code changes. For a changed execution route or role card, run the reading evaluations in `evals/README.md` and record provenance from transcripts. Record actual commands, outputs, and exit codes; a proposed test is not a passing test.
3. Review the resulting diff and counterexamples yourself. Do not delete files, perform large refactors, lower gates, or publish unreviewed changes. Do not use the daily standing approval to release the weekly branch.
4. Add one entry in `docs/owner-actions.md` identifying `weekly/<run key>`, the proposed improvements, evidence and test paths, and the merge/publication decision needed. Deduplicate an existing entry for this key. Leave this entry with the review branch if recording it on main would mix improvement changes into factory records. Notify once and leave the branch unpublished for the owner.
5. Keep main and the next daily run free of these pending edits. Prepare the branch in an isolated review checkout under the repository's normal branch workflow; do not switch the scheduler's primary checkout away from main or overwrite concurrent work. If isolation cannot be prepared safely, leave patches and evidence in the review workspace and record the blocker instead of changing the primary checkout.

Public improvement-history entries and profile/dashboard copy are public changes, not automatic record refreshes. Prepare their wording on the review branch, with the observed baseline and proposed hypothesis, and wait for approval. The weekly runner does not call the daily post-shift public publisher.

## Close

Record next week's hypothesis with a baseline, exact metric/cohort, predicted direction, observation window, and a falsification condition. Name the proposed intervention and whether it is pending owner approval. A week with no defensible intervention may record that result rather than inventing work.

Wait for all writers, check the final evidence, and run `bin/factory shift end --owner <session-name>`. Never release another holder's lease. Report the period, retro path, pending review branch, test results, and the single owner action. The scheduler summary records the served main and sub-agent models, outcome, and failure reason from the transcript and runner, not from a worker's self-report.
