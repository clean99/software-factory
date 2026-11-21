# Session shift

The daily coordinator runs at 21:43 Asia/Singapore through `agent-shift.py`; the weekly review follows `weekly-review.md` on Sunday at 22:30. `schedule.json` owns both kinds' shared execution route and limits. See `README.md` for slot keys, grace periods, locks, idle kill switches, and isolated dry-runs. The next deployment belongs on the Devbox; do not enable a new loop on the Mac. This flow may submit through the factory gates under the standing approval in `lines/oss/policy.md`; only the legacy `run-shift.sh` launcher uses `shift-prompt.md` and remains prepare-only.

## Start
Factory records stay on `main`: use `bin/factory commit -m '<what changed>' --path <path>`, which also pushes, and do not create a shift branch.

1. Run `bin/factory shift start --owner <session-name> --ttl-hours 4`.
2. Exit 1 means stop: read who holds the lease. Even the same name cannot start twice.
3. If continuing your existing shift, use `bin/factory next`, not another start.
4. Complete any saved-main push printed at start before refreshing. Run `bin/factory next --refresh` once to refresh saved PR and waiting-issue observations. Use the runner's background mode, redirect output to a scratch log and save the command's exit code; wait for that same invocation to finish before acting or refreshing again. The sweep overlaps reads across at most four repositories, but slow GitHub responses can still exceed the foreground limit. Read the log and check the saved exit code, not merely that the background process started.
5. Before scouting, preparing, or publishing new work, follow [Public issue inbox](public-inbox.md): read every open issue and its relevant comments, verify maintainer opt-outs, stop and park affected work before acknowledging it, and record complaints or unclear feedback. If the inbox cannot be read, withhold all new contributions; existing follow-up can continue.

## Loop
Work in this order: sync upstream events and feedback; triage the public issue inbox; handle existing contributions and failing CI; then consider at most one qualified new contribution from permitted depth lanes. Reproduce the problem, implement the repair, obtain independent and coordinator review, run required tests, and ship only through the gates under the recorded standing approval. At close, complete “Refresh saved pull request history” before “Refresh profile header”; both queue actions follow waves, release, record commits, and any weekly measurement, and precede the report. One qualified new PR a day is an aim, not a quota: a day with none is valid when the closing record explains why. Never open low-value PRs or nudge maintainers to fill a quota.

On Monday, “Measure the week” is a fallback, not a second review. While holding the lease, check `docs/retro/` before writing: a weekly retro dated within six days closes that queue action. If it is still owed, use the evidence and owner-review boundary in `weekly-review.md` rather than reviving historical volume targets.

Never send messages to Workflow agents: wait for the workflow's result and put follow-up questions into the next wave's brief.

1. Run `bin/factory next` and do exactly the action it prints.
2. Follow its numbered commands and its role brief; do not invent a second queue.
3. Run `bin/factory next` again after the state changes. `--all` previews the order; `--json` exposes the same actions.
4. Only `--refresh` runs a new sweep. Ordinary next reads saved observations and does not advance cursors.
5. The lock is a lease: before a wave, run the printed generation-bound renewal for eight hours. Another session's raw `lock acquire` after expiry is not prevented, even if workers still write. Do not start overlapping waves or release a busy lease.
6. Pass the printed scriptPath and JSON arguments to Workflow unchanged; record provenance for every touched or newly created item afterwards. For your own work or a custom workflow, use the transcript contract in `docs/repairs.md#execution-provenance`.
7. An observable action closes when its evidence exists, not when someone says it is done.
8. For an unobservable result, use `bin/factory ack <id> --note '<result and evidence>'` only if that ID is still pending after `bin/factory next`. Dropping a taken item settles its action without ack; repairs, new heads and refreshed observations can also remove or replace an ID. Never ack an ID that has disappeared.
9. CI ack needs one `JOB: ours|external — evidence` line per job. A known failure name alone is not evidence.
10. A scout with no qualifying candidate uses its printed scout ID for ack. A disk shortage the repair steps cannot clear is acked with the free space left and its cause; it returns next half-day, and installs stay forbidden meanwhile. An observation action explicitly offering deferral can be acked with failure evidence for twelve hours; a repository read failure keeps that repository withheld, and a history refresh that cannot run keeps the header refresh withheld. Never ack a wave, review, ship or other records action to skip it.
11. Merge records use separate destination receipts. Only a real connector failure permits a Private notebook deferral, which returns after twelve hours. The daily profile header refresh follows the saved-history attempt and uses the same scanned push path in `docs/repairs.md#profile-repository`, without a merge receipt; it closes only when the published `origin/main` header matches. A profile merge receipt owed while the history refresh is pending comes after the refresh, so the profile is published once.
12. If a command refuses, read the reason and fix the cause. Never bypass a gate, rewrite historical proof or lower the bar; park the item instead: `bin/factory park <item> --reason '<evidence-backed reason>'` (add `--blocked` when it waits on something outside the factory).
13. Repair procedures are in `docs/repairs.md`. Legal/account actions belong in `docs/owner-actions.md`; notify once and continue unrelated work.
14. Relay the printed Chinese report unchanged, then ack its ID. `idle` means nothing else is owed.

## End
The full suite is required only when the shift changed factory code in `factory/`, `bin/`, `schedule/`, `hooks/` or `tests/`; for record-only shifts, brief, lesson and documentation checks suffice (see `docs/repairs.md`). Run `python3 tests/run_suite.py --jobs 4` in the background with output redirected to a file, and check its saved exit code before calling it successful.

Wait for all wave writers to finish, including on a failure path. Run `bin/factory shift end --owner <session-name>` to release the lease and print the current report. Do not release another holder's lease or use force to recover a stale session.
