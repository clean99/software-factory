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

- There is no open-PR count cap (in total, per repository, or before a first merge) and no daily or per-shift new-PR ceiling; objective conditions decide each contribution: a real unresolved issue, explicit upstream policy, tests and independent review (2026-11-22).
- Each contribution shift aims for `new_prs_per_shift_target` (currently 10) new PRs, counted from the shift's start: the scheduler's run start, else the shift lease's acquisition, else Asia/Singapore midnight. The target is not a ceiling: qualified work continues after it is reached, and a shortfall is reported with its count and reason, never filled with weaker or split work. Each pull request actually created counts once, at its GitHub creation time; updates, retries and reopens do not count, and submit reads the repository's own PR list so a retry never opens a second PR for a branch (2026-11-22).
- Pause new work in a repository only on a long record of unanswered work: at least `unresponsive_min_prs` (currently 3) of our PRs there have waited `unresponsive_days` (currently 30) days or more with no maintainer response, and no maintainer has answered any of our PRs there since the oldest of them opened. One slow PR, a short wait or a small sample never pauses a repository; the reason names the PRs and the pause lifts when a maintainer answers (2026-11-22).
- GitHub rate limits pace the work: wait for the reset GitHub reports, fetch only the fields and pages a check needs, and reuse one fetch across checks and workers. A rate limit never disqualifies a repository, and no other token or account is used to get around it (2026-11-22).
- Refresh stored qualification after `qualify_max_age_hours` (currently 12) hours; this does not replace uncached collision checks immediately before coding and new-PR publication (2026-10-24).
- Require a lesson for closed work before new submissions, and write one for maintainer silence beyond `silent_days` (currently 7) days; community or bot activity does not replace a maintainer response (2026-10-26). One PR's silence does not stop other submissions (2026-11-22).
- Keep small-item source changes within `max_diff_lines` (currently 400) and approved large-item source changes within `max_diff_lines_large` (currently 900); split larger work into independently safe layers (2026-10-25).
- Re-examine scope above `warn_diff_lines` (currently 200) changed source lines and test size above `warn_test_lines` (currently 1200) changed test lines; these are warnings, not quotas (2026-10-25).
- Keep PR prose concise; `max_pr_body_lines` (currently 45) non-empty body lines is a warning threshold, not a substitute for the repository's template (2026-10-24).
- Rewrite prose when style findings exceed `max_soft_slop_hits` (currently 3); hard hygiene failures remain blockers regardless of this count (2026-10-24).
- Scouting has no per-shift count: each active lane is scouted at most once per shift, whether the hunt created an item or was acknowledged with no candidate (2026-11-22).
- Use `review_budget_minutes` (currently 120) minutes as the default review budget unless the item specifies its own; overdue unfinished or stale reviews require coordinator takeover (2026-11-10).

## Selection

- Require real user demand and a positive user-facing effect for every contribution; interview value, contribution share, a constructed probe or a self-filed issue alone is insufficient (2026-11-02).
- For failure and edge paths, require a real report, live capture or reproducible provider-emitted occurrence; a documented event type alone is not evidence of someone hitting it (2026-11-04).
- Read documented semantics and workarounds before treating long-shipped behavior as a regression; resolve design trade-offs with the maintainer rather than disguising them as small fixes (2026-11-03).
- Prefer named follow-ups on our merged work, maintainer-endorsed goals and useful series in the same code area over racing for loose bugs (2026-11-07).
- A bug report's age is not a reason to skip it. Check it for an open or linked PR, a claim, an assignee or a maintainer already working on it when starting, after implementing and right before ship (2026-11-22).
- Take lane state from `lanes.json`. Among active depth lanes not paused for unresponsiveness, scout the least recently scouted repository first, lane order breaking ties. There is no cooldown: a repository whose last scout found nothing is scouted again in the next shift (2026-11-22). Paused or stopped lanes get no new hunts (2026-11-20 weekly retro).
- Explore every active exploration lane, with no per-shift exploration cap, when every active depth lane is paused or produced no suitable work in this shift (2026-11-22). Find a concrete unresolved issue first, then read that repository's AI-contribution policy, current rules and outsider-PR record (2026-11-22).
- When every active lane is cleared and the shift is still short of its new-PR target, look for new repositories with `skills/oss-opportunity-scout`: start from concrete unresolved issues, then read each candidate's contribution guide, AI-contribution policy, CLA or DCO terms and automation limits, apply the outsider-PR record test below, and add only qualifying repositories to the exploration lanes in `lanes.json` with a dated reason. A repository that bans or restricts automated contributions is not a candidate (2026-11-21). One whose AI policy only asks a person to take responsibility for, review or stand behind each AI-assisted submission is a candidate under the owner's standing endorsement, recorded per repository with `bin/factory policy <repo> --endorse` after reading the live rules. The command accepts only the sentences listed in `factory/oss/policy.py` (`COVERED`), so other wording needs a reviewed code change first; human-written prose and prior-approval rules stay excluded (2026-11-22).
- Before a first PR, run `bin/factory health <repo> --responsiveness`. In one GraphQL query it reads up to 50 PRs opened between `responsiveness_record_days` (currently 180) days ago and `unresponsive_days` ago, and counts outside contributors' PRs that got a maintainer response (a review, a comment, a merge or a close) within `unresponsive_days`. Skip the repository for unresponsiveness only when the sample holds at least `responsiveness_min_outside_prs` (currently 10) outside PRs from at least `responsiveness_min_outside_authors` (currently 5) authors and fewer than `responsiveness_min_response_percent` (currently 20) percent got such a response; a smaller sample is not enough history to reject it. A maintainer's invitation on the issue overrides the test. Record the sample, the response share and the window with the decision (2026-11-22).
- Star counts, popularity and repository size rank candidates; they never reject one (2026-11-22).
- Keep a repository record in `ledger/repo-records/` for every shortlisted repository: its policy scan and outsider-PR record, each with the time it was read. Reuse the policy scan for `policy_record_max_age_hours` (currently 168) hours and the outsider-PR record for `responsiveness_record_max_age_days` (currently 30) days unless the rules that produced it change, and scout the shortlist for new or updated issues and PRs instead of re-screening whole repositories. Release rereads the policy when the item's scan is older than `qualify_max_age_hours`, so a saved scan never hides a later upstream change (2026-11-22).
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

- Run three daily coordinator shifts at 05:43, 13:43 and 21:43 Asia/Singapore and one weekly review on Sunday at 22:30 in the same zone, through the single scheduler configured in `schedule/schedule.json`. No shift runs alongside the weekly review or another shift: a busy daily slot is skipped and never replayed. A contribution shift the owner asks for now runs through `run --manual` under the same locks (2026-11-22). All formal shifts use the same coordinator and sub-agent routes. The scheduler runs on one machine at a time; moving it is a separate migration that the owner confirms, and no second loop is started in the meantime (2026-11-21).
- Sync upstream events and feedback, then read the configured public repository's open issues before new work. Follow `schedule/session-shift.md` for verified maintainer opt-outs: stop the lane, park prepared items, acknowledge through the scanned own-repository path, and record the result. Bad-PR feedback becomes a lesson candidate and an owner action; unclear feedback gets one owner notification. Handle existing contributions and failing CI before selecting new work (2026-11-21).
- Aim for ten new pull requests in every contribution shift, timed or manual. Each shift has its own count, and a shortfall does not carry to the next shift. Ten is a target, not a ceiling: qualified work continues after it is reached. A shift short of ten is valid when its count and reason are recorded. When the permitted lanes run dry, look for valuable new repositories and issues as `schedule/session-shift.md` describes. Never lower the quality bar, split or duplicate work to raise the count or nudge maintainers to fill the target (2026-11-22).
- Hold the shift lock, keep its lease valid while work runs and release it on exit; do not start another writing wave in the same repository concurrently (2026-11-10). The weekly review waits for the daily runner and lease without preempting either, within its grace window (2026-11-21).
- Follow `schedule/weekly-review.md`: record data refreshes and the dated retro as factory records; prepare production code, policy, role, template and public-copy improvements on `weekly/<run key>` with tests and one owner action, then wait for owner review. No deletions, large refactors or unreviewed publication (2026-11-21).
- Keep Monday's “Measure the week” queue action as a fallback. A retro page dated within six days closes it, so the weekly review and fallback do not both write a retro. Lane rotation still needs comparable two-week evidence, not a submission quota (2026-11-21).
