# Current policy

Rules in force as of 2026-11-21. [Decisions](decisions.md) preserves the dated source statements; [factory.config.json](../../factory.config.json) owns numeric limits. Repository-specific restrictions can be stricter and remain on repository cards and in [lanes.json](lanes.json).

## Publishing and approval

- Publish issues, PRs, comments and replies through the appropriate factory wrapper after its gates pass, citing the standing approval rather than requesting per-item approval; approval stays bound to the actual head and public text (2026-10-25).
- Contributions are openly automated: ship adds disclosure to every new PR from 2026-11-20. Use upstream disclosure formats; skip repositories that ban or restrict AI-generated or AI-assisted contributions. Never claim human review. Existing PRs and replies are not retroactively changed unless a maintainer asks. Commits follow upstream conventions without attribution trailers. See `docs/direction.md` (2026-11-20).
- For issue-first work, prepare the fix and all non-issue release evidence before opening the issue, then publish the prepared PR promptly unless that repository explicitly requires waiting for a maintainer reply (2026-10-31).
- Tick Code of Conduct acknowledgements on the owner's behalf; this does not authorize legal signatures (2026-10-26).
- Tick template self-declarations only when the statement is true and supported by the completed checks and independent review; never invent having read, understood or tested something (2026-11-02).
- Continue gated own-project work and releases in parallel with contributions; the initial two approved releases do not need to wait for trial users, and the separate product-line requirements otherwise still apply (2026-10-26).

## Caps

- Repositories without a first merge share `max_open_prs_total` (currently 20); repositories that have merged our work do not consume that exploration pool (2026-10-27).
- Before a repository's first merge, allow `max_open_prs_per_repo_before_first_merge` (currently 1) open PR (2026-10-25).
- After a repository's first merge, allow `max_open_prs_per_repo` (currently 3) open PRs, subject to stricter repository-card or paused-lane restrictions (2026-10-27).
- Daily new submissions have `max_new_prs_per_day` (currently 1), counted by Asia/Singapore calendar day regardless of the machine zone; this is a ceiling, not a quota, and repository caps and all quality gates still apply (2026-11-21).
- Stop new submissions when a PR has been open for `stale_open_days` (currently 3) days and none of our work in that repository has merged since it opened; resume only when the stale condition clears and the lane permits work (2026-10-31).
- Count open PRs within `open_pr_window_days` (currently 180) days for the cap; do not treat older unrelated work as current exploration inventory (2026-10-25).
- Refresh stored qualification after `qualify_max_age_hours` (currently 12) hours; this does not replace uncached collision checks immediately before coding and new-PR publication (2026-10-24).
- Require a lesson for closed work or maintainer silence beyond `silent_days` (currently 7) days before new submissions; community or bot activity does not replace a maintainer response (2026-10-26).
- Keep small-item source changes within `max_diff_lines` (currently 400) and approved large-item source changes within `max_diff_lines_large` (currently 900); split larger work into independently safe layers (2026-10-25).
- Re-examine scope above `warn_diff_lines` (currently 200) changed source lines and test size above `warn_test_lines` (currently 1200) changed test lines; these are warnings, not quotas (2026-10-25).
- Keep PR prose concise; `max_pr_body_lines` (currently 45) non-empty body lines is a warning threshold, not a substitute for the repository's template (2026-10-24).
- Rewrite prose when style findings exceed `max_soft_slop_hits` (currently 3); hard hygiene failures remain blockers regardless of this count (2026-10-24).
- Limit scouting to `max_hunts_per_shift` (currently 3) repositories per shift, counting both new items and acknowledged hunts with no candidate (2026-11-20 weekly retro).
- Use `review_budget_minutes` (currently 120) minutes as the default review budget unless the item specifies its own; overdue unfinished or stale reviews require coordinator takeover (2026-11-10).

## Selection

- Require real user demand and a positive user-facing effect for every contribution; interview value, contribution share, a constructed probe or a self-filed issue alone is insufficient (2026-11-02).
- For failure and edge paths, require a real report, live capture or reproducible provider-emitted occurrence; a documented event type alone is not evidence of someone hitting it (2026-11-04).
- Read documented semantics and workarounds before treating long-shipped behavior as a regression; resolve design trade-offs with the maintainer rather than disguising them as small fixes (2026-11-03).
- Prefer named follow-ups on our merged work, maintainer-endorsed goals and useful series in the same code area over racing for loose bugs (2026-11-07).
- Do not hunt bug reports younger than 48 hours unless a maintainer invited a PR there; there is no three-hour completion exception (2026-11-10).
- Take lane state from `lanes.json`. Among active depth lanes with a free slot, scout the least recently scouted repository first, lane order breaking ties; a repository whose last scout found nothing cools down for three days unless new upstream activity arrives. Paused or stopped lanes get no new hunts (2026-11-20 weekly retro).
- Explore at most one repository per shift from active exploration lanes, when every active depth lane is cooling down, has no free slot or produced no suitable work in this shift; read contributor health, the AI-contribution policy and current rules first (2026-11-20 weekly retro).
- Before a first PR, sample outsider PRs from the last two weeks for human review within 3 days; skip a repository failing this test unless a maintainer invited a PR, regardless of older merge or health statistics (2026-11-01).
- Keep silent repositories stopped without pings or duplicate submissions, and aim for a substantial maintainer-backed design trade-off each week without padding the diff (2026-11-02).
- Select the additional own projects for useful demand and growth potential, preferring faithful ports of well-maintained projects whose tests can serve as the oracle (2026-10-25).

## Review

- Complete repository-required lint, formatting, typechecks, builds and tests and prove the regression fails without the fix; a declaration that tests passed is not evidence (2026-10-24).
- Obtain independent review bound to the current head; same-model fresh-context review is only a warning-level exception for small items, never for large items (2026-10-24).
- Workers may implement, but the coordinator must review every head and changed public text before publication and take over or park work after a second fix-needed review or an over-budget review (2026-11-10).
- For schema-2 items, have a different reviewer decide build, propose or drop from demand evidence before implementation, then answer every review risk class with applicable head-bound receipts (2026-11-10).
- For schema-2 items, capture command results through `factory run`; the coordinator reruns the required proofs and an additional counterexample before release, while legacy items retain their existing proof formats and all items require coordinator review (2026-11-10).
- Re-read the live PR's state, head, commits and comments before replies and follow-up pushes; stop and re-plan on closure, merge or someone else's changes (2026-10-28).

## Records

- Keep the ledger authoritative for PR outcomes and never bulk-rewrite item status to match it (2026-11-10).
- Update the profile immediately when a merge is observed rather than holding it for the end of the shift (2026-11-07).
- Keep a destination receipt for profile, teaching and knowledge records; deferred or unverified work remains owed, and teaching work does not delay a ready profile update (2026-11-10).
- Record closed-but-adopted contributions as `adopted_in` on their ledger rows; only actual merges count in merge metrics (2026-11-10).
- Present merged work and released projects with concrete user impact, grouped by area and repository, without star-count padding or private identity details (2026-11-02).
- Capture lessons from closed or silent PRs and meaningful maintainer feedback, then route them to checks, role briefs, repository cards or procedures rather than asking every worker to read the history (2026-11-10).
- Remove regenerable dependencies after submission and safely prune unused checkouts after merge or closure, preserving tracked fixtures, shared stores and unpushed work (2026-10-25).

## Identity and privacy

- Use the configured public name and GitHub noreply identity for new commits; the previous-address compatibility is for old items, not a choice for new work (2026-10-25).
- Scan all outgoing prose, branches, commits, identities and artifacts before publication; own-project releases also require a full-history scan, and incomplete scans or findings block publication (2026-10-25).
- Inspect every binary visually and document exact-string exceptions with reasons; for images, run strip-image, inspect the cleaned files and publish only those bytes through the scanned media repository and scanned push wrapper, never browser uploads; record byte-bound binary reviews, since visual review cannot waive metadata and text scans cannot review pixels (2026-10-25).
- Preserve machine and repository hooks without bypasses, and run installs, tests and builds with `CI=1 HUSKY=0 LEFTHOOK=0`; report hook drift or a missing baseline instead of changing security settings (2026-10-31).
- Check the scanner's own exit status, not the last command in a pipeline, and strip signed image-URL queries from captured public text before saving it (2026-11-03).

## Owner-only actions

- Leave CLAs, DCO and other legal agreements, account or repository settings, credential changes, real-account deployments and social-account posts to the owner; social launch drafts still need their approval (2026-10-26).
- Where contribution policy requires the contributor's own words, have the owner supply the substantive public text and only translate it faithfully; self-declaration authority does not override this (2026-11-02).
- Record owner-only work in `docs/owner-actions.md` as soon as it appears, notify once for each new action and continue other work without blocking on questions (2026-10-26).
- Treat unresolved CLA checks as signature blockers rather than maintainer silence, recheck them during follow-up and send no second PR to that repository until signing is resolved (2026-11-06).

## Cadence

- Run one daily loop at 21:43 Asia/Singapore and one weekly review on Sunday at 22:30 in the same zone, through the single scheduler configured in `schedule/schedule.json`. Both use the same coordinator and sub-agent routes. The scheduler runs on one machine at a time; moving it is a separate migration that the owner confirms, and no second loop is started in the meantime (2026-11-21).
- Sync upstream events and feedback, then read the configured public repository's open issues before new work. Follow `schedule/session-shift.md` for verified maintainer opt-outs: stop the lane, park prepared items, acknowledge through the scanned own-repository path, and record the result. Bad-PR feedback becomes a lesson candidate and an owner action; unclear feedback gets one owner notification. Handle existing contributions and failing CI before selecting new work (2026-11-21).
- Aim for one qualified new pull request a day. A day or shift with none is valid when its reason is recorded; volume is never a target. Never lower the quality bar or nudge maintainers to fill a quota (2026-11-21).
- Hold the shift lock, keep its lease valid while work runs and release it on exit; do not start another writing wave in the same repository concurrently (2026-11-10). The weekly review waits for the daily runner and lease without preempting either, within its grace window (2026-11-21).
- Follow `schedule/weekly-review.md`: record data refreshes and the dated retro as factory records; prepare production code, policy, role, template and public-copy improvements on `weekly/<run key>` with tests and one owner action, then wait for owner review. No deletions, large refactors or unreviewed publication (2026-11-21).
- Keep Monday's “Measure the week” queue action as a fallback. A retro page dated within six days closes it, so the weekly review and fallback do not both write a retro. Lane rotation still needs comparable two-week evidence, not a submission quota (2026-11-21).
