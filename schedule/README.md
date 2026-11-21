# Scheduled shifts

`schedule/agent-shift.py daemon` is the one scheduler. It runs the daily coordinator flow and the weekly review from `schedule/schedule.json`; do not add launchd, cron, session cron, or another scheduling service.

The next deployment is on a development machine (Devbox), in a separate migration task. Do not enable a replacement loop on the Mac. Both kinds use the same coordinator and sub-agent routes; exact IDs stay in `schedule.json`.

| Kind | Slot in Asia/Singapore | Limit | Grace |
| --- | --- | --- | --- |
| daily | Every day, 21:43 | 240 minutes | 90 minutes |
| weekly | Sunday, 22:30 | 180 minutes | 8 hours |

The 08:43 slot is removed. `zoneinfo` computes slots in each kind's configured zone, regardless of the machine zone. The daily flow is in `session-shift.md`; weekly analysis and the owner-review boundary are in `weekly-review.md`. The daily coordinator may publish through the factory gates under the standing approval in `lines/oss/policy.md`. Only the legacy `run-shift.sh` / `shift-prompt.md` launcher is prepare-only and never submits, proposes, or replies. Its old launchd template is retained for history, not installed by this scheduler.

## Inspect and run

```sh
python3 schedule/agent-shift.py plan
python3 schedule/agent-shift.py plan --now 2026-11-21T08:10:00+08:00
python3 schedule/agent-shift.py status
python3 schedule/agent-shift.py run --kind daily
```

`plan` prints the next slot, run key, enabled state, models, and limit for each kind as JSON without touching scheduler state. The CLI disables Python bytecode writes before importing factory modules, so `plan` and `doctor` also leave a fresh checkout unchanged. `--now` must carry an offset. Manual `run` uses the latest slot at or before now, not the next slot; outside grace it records a missed run rather than starting arbitrary catch-up work. `--minutes` remains available for manual runs. The claim saves this override, so a deferred run keeps its limit across configuration reloads and daemon restarts; enabled-state and grace checks still use the current configuration. The old `--name` flag is no longer accepted; names are derived from run keys so a manual name cannot overwrite another run's records.

Only the later migration task may start the replacement, on the Devbox, after owner release confirmation. Keep the Mac daemon untouched during preparation. The existing Mac daemon was terminal-started and does not survive reboot; macOS privacy protection can block launchd jobs from reading `<workspace>` (lesson L-008).

## Portable runtime

`runtime` in `schedule.json` holds machine presets. Environment overrides take precedence; `~` uses the current home and `{checkout}` uses this script's checkout. Executables may be command names on the configured PATH or absolute paths; use absolute paths for the router and logs. These defaults preserve the current Mac environment, not a verified Devbox installation.

| Field | Environment override | Default |
| --- | --- | --- |
| `path` | `FACTORY_SHIFT_PATH` | `~/.local/bin`, Node `v24.16.0` and `v20.20.2` under `~/.nvm/versions/node`, then `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin` |
| `python` | `FACTORY_SHIFT_PYTHON` | Empty uses the scheduler's current interpreter for the router; invoke the scheduler itself with Python 3.12 or newer |
| `claude` | `FACTORY_SHIFT_CLAUDE` | `~/.local/bin/claude` |
| `router` | `FACTORY_SHIFT_ROUTER` | `~/.local/bin/factory-model-router` |
| `router_port` | `FACTORY_SHIFT_ROUTER_PORT` | `8794` |
| `shell` | `FACTORY_SHIFT_SHELL` | `/bin/zsh` |
| `notify` | `FACTORY_SHIFT_NOTIFY` | `{checkout}/bin/factory`; called with `notify <message>` (an alternative executable must accept the same arguments) |
| `logs` | `FACTORY_SHIFT_LOGS` | Empty uses `shift-logs` beside the configured contribution clones |

The configured PATH is used for GitHub preflight, sessions, the router, public refresh, and notifications. Put the Devbox's Python and Node directories there. Do not copy a Mac executable or assume its Node/Python paths exist on Linux. A missing session binary, interpreter, shell, or router records a failed run with one prerequisite message before GitHub preflight; it does not retry or emit a traceback.

`python3 schedule/agent-shift.py doctor` prints a read-only JSON report and exits 1 if a prerequisite is missing: Python 3.12+, Singapore zone data, git, gh, gh authentication, gitleaks, configured runtime tools, log-directory writability, a clean main checkout, and the effective schedule. Authentication reports only the exit code of `gh auth status`, never its output. Writability uses an access check on the directory or nearest existing parent, without creating anything; it is not proof against disk exhaustion or a later permission change. Doctor does not acquire a lease, start the router, or launch a session.

Run doctor and both model probes on the Devbox before activation. A successful Mac probe proves nothing about the Devbox's installation or credentials. Set a Devbox runtime preset there, not by changing global harness settings. Do not edit runtime paths while a run is active; log-directory changes require a stopped scheduler and a deliberate state migration so keys and locks stay together.

`FACTORY_SHIFT_MODEL` and `FACTORY_SHIFT_TIMES` are removed. Setting them or any undocumented `FACTORY_SHIFT_*` variable fails explicitly. Edit the per-kind settings for models and times.

## Run identity and locks

Daily keys are `daily-YYYY-MM-DD`, using the slot's local date. Weekly keys are `weekly-YYYY-Www`, using its ISO week-year. Before launch, the runner exclusively creates `<logs>/runs/<key>.json`. A second trigger for that key, including a manual invocation or daemon restart, prints `already claimed` and exits 0. The record transitions through `claimed`, `running`, and `finished`, `failed`, or `skipped`, retaining the reason and timestamps. A waiting weekly run remains `claimed` with `deferred_at`; waiting is not another session attempt.

The owner's explicit `run --kind daily --force-key` creates a separate `<key>-force-<id>` attempt. It preserves the original record and still obeys the kill switch, locks, lease, and grace. Use it only after inspecting a failed or abandoned run and confirming all its writers have stopped. Never delete run keys to retry silently.

The daemon holds `flock` on `<logs>/scheduler.lock`; the heartbeat pid is for humans, not mutual exclusion. `<logs>/active.lock` serializes scheduled and manual runs before either can acquire the factory lease. Each key also has a lock for resuming deferred work. The existing `<name>/runner.lock` stays held through summary writing, and `bin/factory shift start` still owns the factory lease. Keep lock files: unlinking one while it is held defeats the lock.

The daily runner retains generation-checked recovery of an unlocked old `auto-` lease at its final preparation check. It never releases a live runner or another kind of owner's lease. The weekly runner does not perform stale-lease recovery: at Sunday 22:30, it waits while the daily lease or runner lock is held, then launches once when both are free. Its claim survives restart. A lease acquired by another coordinator during GitHub preflight or router startup also defers the weekly claim if no session has started. The runner releases its execution locks and waits without releasing that coordinator's lease. If grace closes first, it records `skipped`, reason `missed`, and notifies. It never preempts the daily shift.

## Missed slots and stopping

Within grace, a late slot runs once. After grace, it is skipped with reason `missed` and a notification. After a long gap, only the newest slot per kind is eligible; older slots since the saved cursor are marked skipped. If both kinds are eligible on wake, only the newer slot runs; the other is skipped. There is no chain of catch-up runs. A first start has no history to reconstruct, so it considers only the latest slot per kind. Existing claims are never replayed automatically.

Set `kinds.daily.enabled` or `kinds.weekly.enabled` to `false` to pause one kind. Set top-level `enabled` to `false` to make the daemon idle. It rereads configuration every 30 seconds and keeps writing its heartbeat. Starting it with `FACTORY_SHIFT_DISABLED=1` also idles both kinds; changing an environment variable in a different shell cannot change a running process. The runner rechecks these switches after GitHub preflight and after router startup, immediately before the first session launch. A disabled claim becomes `skipped` without launching a session or refreshing the public site. Neither switch kills a session already running; its process-group deadline still applies. Slots reached while disabled are recorded skipped, not saved as a backlog.

`summary.json` includes kind, key, requested models, observed `models.main` and `models.subagent` arrays, status, and reason. Observed models come only from assistant messages in the transcript; empty arrays mean no served model was observed, never a guess from configuration. Arrays preserve fallback changes. Failures and skipped runs notify through `bin/factory notify`. The heartbeat lists each kind's enabled state, next slot with offset, last key, status, and reason.

An exit 0 finishes a run only if its own shift lease is closed. Otherwise the runner resumes the same session within its attempt and time limits to finish background work. On exhaustion or timeout it stops the process group, records failure, and releases only its own lease. Weekly runs never invoke the post-daily public refresh: unreviewed weekly changes must not reach the public export.

## Isolated verification

```sh
FACTORY_SHIFT_LOGS="$PWD/workspace/schedule-check" python3 schedule/agent-shift.py run --kind daily --dry-run
FACTORY_SHIFT_LOGS="$PWD/workspace/schedule-check" python3 schedule/agent-shift.py run --kind weekly --dry-run
```

Dry-run requires an explicit non-production log directory. The default production directory and its descendants are refused, as are custom scheduler directories and their descendants, identified by scheduler or execution-lock markers. Before writing, dry-run and probe also validate their mode-specific destination and reject symlinks anywhere in its existing state or output tree. Use a fresh verification directory rather than removing lock files. Do not change that directory's structure while verification runs.

Dry-run stores its claims, locks, and summaries under `<logs>/dry-run/`, with keys prefixed `dry-run-` and `dry_run: true` saved at claim time. It cannot consume a production key. The daemon excludes verification records from recovery, pending work, and heartbeat history, and execution rejects a record whose saved mode differs from the requested mode. A deferred verification run stays a dry-run; the invoking command, not the daemon, waits for admission.

Dry-run takes the locks, checks the lease without writing its sidecar, starts the local router, and records routing without launching a session, contacting GitHub, or publishing. It ignores slot age so routing can be checked outside grace; the real run does not. Repeating a key in the same verification directory demonstrates the duplicate no-op. Dry-run notification lines print locally rather than sending a notification.

`probe --kind daily|weekly` uses the same router and model environment, with a single four-turn session in an empty scratch directory. Tools are limited to Agent, customizations and MCP are disabled, and the prompt only asks for READY and one delegated READY. It forwards sub-agent messages and reads their actual `message.model` values. Probe requires isolated logs and reserves a `probe-<kind>/once` marker; it never retries, even on authentication failure. Save its output and resolve login errors with the owner rather than entering login loops.

Monday's existing “Measure the week” queue action remains the fallback. `factory/core/nextaction.py` suppresses it when a retrospective page is dated within the preceding six days; the Sunday weekly page satisfies that check. Both procedures check for the current period's page while holding the same lease before writing, so they do not duplicate the retro.
