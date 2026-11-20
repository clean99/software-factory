# Shift repairs

These are conditional procedures, not permission to change settings or bypass a gate. Read the current action and live evidence first. Preserve other work and partial results. A failed check is not an instruction to weaken it. Use configured factory/clone paths; placeholders below must be replaced with the actual current item and repository.

## Locked keychain

On macOS, git error `-25293` means the login keychain is locked or unavailable to the unattended process. It can also cause `could not read Username` and make `gh auth status` report an invalid token. Do not diagnose expired login from those messages or add an owner action. The runner checks authenticated GitHub API and git access before starting a session or touching the lease, retrying for up to twenty minutes; an unavailable check ends with a blocked status in summary.json.

If access disappears during a shift, save its selected records with `bin/factory commit -m '<what changed>' --path <path> --no-push`, wait for its writers, and close the shift normally. Do not keep retrying publication or ask the owner to log in again. At the next accessible shift, next prints the saved main-branch push before new work.

## Disk space

The home volume needs at least 6 GiB free. Coordinate with any running wave before cleanup; never delete its resources.

1. Run `bin/factory clone release --idle` first. It preserves branch recovery records and bundles while freeing idle checkouts; inspect refusals and keep active writers' resources. After a merge or close, `bin/factory clean --prune` still removes unused regenerable resources. Do not manually remove dependency directories: some repositories commit dependency fixtures.
2. Still below 6 GiB: inspect the factory's pnpm 11 store. With Node 24 on PATH, `npm exec --yes --registry=https://registry.npmjs.org --package=pnpm@11.24.0 -- pnpm store prune` removes packages no project links. Do not prune older pnpm stores or the npm cache used by unrelated work.
3. Still below 6 GiB: inspect gitignored `workspace/` top-level scratch entries older than three days. Exclude `repos`, `runs`, `tools`, `toolchains`, `node*`, every entry named by a live item or running wave, and anything whose ownership is unclear. Remove only reviewed, regenerable scratch entries. `uv cache prune` can remove unreferenced Python wheels.
4. Recheck free space. If it remains low, do not broaden the deletion scope: record the blocker once in `docs/owner-actions.md`, then acknowledge the printed ID with `bin/factory ack <id> --note '<N> GiB free after <what was cleaned>; <what holds the rest>'`. The acknowledgement lasts for the current UTC half-day. Until space returns, do only work that needs no install, build or test run.

## Clone recovery

Release keeps item evidence and every commit reachable from a local branch or tag. Commits missing from origin go into a verified Git bundle in `archive_dir`, defaulting to the `archive` sibling of `repos_dir`, outside the checkout. The bundle needs origin's history to restore. Release refuses when there are uncommitted or untracked files, stash entries, linked worktrees, detached commits, or a submodule with work of its own. Remote or bundle check failures also keep the checkout in place. Ignored files are deleted with the checkout; move precious git-ignored files, such as a local `.env`, out before release. Keep the bundles until their saved branches and tags are safely available elsewhere.

For unresolved deltas or index-pack errors, run `bin/factory clone-heal <clone> --dry-run`, inspect the retained branch/worktree plan, then `bin/factory clone-heal <clone>`. Preserve unpushed commits and dirty files. Never force cleanup around a live writer.

A missing clone with an item.json `released` record is normal idle storage, not a repair: planning uses its recorded head, and code-work commands restore it on demand. Use `bin/factory clone ensure --item <item>` for explicit restoration. Without that record, read item.json, recover the exact clone and branch using its existing fork/upstream records, and verify the head. Do not substitute a fresh default branch for the reviewed head. If the clone cannot be safely recovered, park with a reason. A malformed item or unknown branch belongs in the repair bucket, not a new hunt.

## Repository observation failures

A failed recent-mergers read can misclassify a private maintainer as a community member. Refresh retries the repository even without active PRs. Recovery uses successful login-bearing reads to reclassify saved feedback without rewriting history; a producer's completion and source key keep overlapping readers and already-replied feedback safe. The feedback replay cursor is separate from the PR/CI observation clock.

If the read still fails, use the observation action's printed `ack` command with the actual failure evidence. This defers the repository's currently pending warnings for twelve hours, including failures added by the printed refresh, not completion. A later new failure returns immediately. The affected repository's code work and scouting stay withheld; unrelated repositories and records continue. Ordinary item repairs and release gates cannot be acknowledged away.

## CI triage

Read every failing job log, including its matrix version, before editing code. Compare the same job and error signature on the current base branch and other PRs. Preserve CLA as a separate owner blocker. Record one line per job in the ack note: `JOB: ours|external — log URL/run, version, error and base comparison`. An incomplete investigation stays pending.

Known leads, never a waiver by name:

| Repository / check | What to compare |
| --- | --- |
| Deployment previews / Vercel | A current “Authorization required to deploy” response; distinguish account authorization from a build failure in our code. |
| litellm / osv-scan | The unchanged lockfile and the same scanner finding on base; a different finding still needs investigation. |
| Lexical / collab-windows Firefox | Same Windows/Firefox failure on base and comparable PR runs. |
| Any / cancelled duplicate run | The latest completed run of the same workflow/check must pass; ties and missing timestamps are not proof. |
| Knip / TypeScript matrix | Run the exact failing TypeScript version and CI command; the default local version does not cover a TypeScript 5.0.4 failure. |

Failures in our changed files are ours until evidence shows otherwise. Reproduce the exact CI step, fix on the same branch, capture the checks, obtain fresh independent and coordinator review, and use the open-PR update path below. Do not merge main or bump a silent PR just to make checks green.

## Stale PR head

Distinguish a stale GitHub PR display from somebody else's new branch commit. Read the live PR state/head and the actual fork branch first. If another writer moved the branch, stop and reconcile; do not overwrite it.

1. Complete current-head independent and coordinator review and approval, then run `bin/factory submit <item> --dry-run` and `bin/factory submit <item>` to push through the checked path.
2. Read `bin/factory gh -- pr view <url> --json state,headRefOid` again.
3. Only when the PR is still OPEN, the fork has exactly the reviewed expected head, and GitHub still displays an older head, close and reopen with `bin/factory gh --item <item> -- api -X PATCH repos/O/R/pulls/N -f state=closed`, then the same command with `-f state=open`.
4. Confirm headRefOid equals the reviewed fork head, then run `bin/factory next`; ack with that evidence only if the same ID remains pending. Never close/reopen a merged or independently closed PR.

## Waiting issues

For ISSUE TAKEN, read the referenced competing PR and determine whether it actually solves this issue. A cross-reference alone is not a collision. If it does, set the local item to dropped with the competing URL and reason; this settles the action without ack. Do not publish a duplicate. If the reference is unrelated, run `bin/factory next` and ack only the still-pending ID with the source and decision.

For ISSUE CONFIRMED, read the maintainer's actual invitation and any design constraints, check for a new claimant/PR, then resume the existing item. Run `bin/factory brief review --item <item>` when its head needs review, followed by coordinator review. Only when the item is ship_ready run `bin/factory ship <item> --dry-run` and `bin/factory ship <item>`. Run `bin/factory next` and ack the disposition only if the same ID remains pending; an invitation never bypasses release checks.

## Open PR updates

Follow tasks prepare code and reply text; they do not publish. Preserve the event_keys supplied by next in follow.json so later comments cannot be confused with already handled requests. Map every request to a disposition and evidence. Re-read live OPEN state and head before acting; a merged or closed PR leaves the follow queue.

1. Before any push, select the inline root comment databaseId or conversation destination. A review-level ID is not an inline comment ID. Check earlier maintainer decisions before accepting a bot finding.
2. Complete independent review, then personally run `bin/factory brief coordinator-review --item <item>`. Read the complete diff, tests, demand/impact evidence, PR and reply text; rerun base/head proofs and apply justified wording nits. A worker's ship verdict is not a coordinator verdict.
3. Run `bin/factory approve <item> --note '<applicable standing approval from config>'`, then `bin/factory submit <item> --dry-run` and `bin/factory submit <item>`. Approval is head-bound. Do not use generic push after merging/rebasing onto upstream: submit refreshes the target and excludes its public commits, while generic push can scan unrelated upstream history or fail on blobless objects.
4. If pr.md changed, write a body file in the item directory without its title line. Run `bin/factory gh --item <item> -- pr edit <N> -R <owner/repo> --body-file <body-file>`. Body edits still require the release gate at the current body hash.
5. Re-read the live PR immediately before replying. Run `bin/factory approve-reply <item> --file <reply-file> --note '<applicable standing approval>'`. Use `bin/factory thread-reply <item> --comment-id <inline-root-id> --file <reply-file>` for an inline thread, or `bin/factory reply <item> --file <reply-file>` for the conversation. The reply filename is relative to the item directory. Never add force to evade a changed head.
6. Confirm the live body, headRefOid and reply destination. submit records pushed_head; the reply wrappers record the reply URL and time. Reply-only work still receives text review and approval, but does not need a gratuitous code push.

## Profile repository

Update the merged-work block in the owner's profile repository, not its open-PR count. Use the configured GitHub login for both owner and repository name. Clone over HTTPS with full history: an SSH remote can be misread as an address by the privacy scanner, and shallow history prevents complete outgoing scans. If a clone already exists, inspect its remote/history and dirty state rather than replacing it.

1. `bin/factory sync profile` restores the configured profile checkout when absent, including when --readme explicitly names its README.md; for a different location, use `git clone https://github.com/<login>/<login>.git <profile-clone>` only if absent, without a depth option.
2. Edit the matching `groups[].repos[]` entries in `lines/oss/profile-groups.json`: update each `blurb` to describe newly accepted work while preserving earlier contributions; add a repo entry when absent. A joined entry shares its preceding blurb; an `outside_block` entry needs a hand-maintained README edit instead. `bin/factory sync profile --readme <profile-clone>/README.md` then previews counts and those curated descriptions, not newly written prose. Compare both with accepted merges. Renames use repo_renames.
3. `bin/factory sync profile --readme <profile-clone>/README.md --write` applies only the generated block. Preserve unrelated prose.
4. Inspect `git -C <profile-clone> diff -- README.md`, then `git -C <profile-clone> add -- README.md`. Refuse unrelated staged changes.
5. Run `gitleaks protect --staged --redact --no-banner --ignore-gitleaks-allow --gitleaks-ignore-path /dev/null --source <profile-clone> --config <factory>/factory/core/gitleaks.toml`. A scanner failure is a stop, not a clean result.
6. Commit only README.md using `git -C <profile-clone> -c user.name='<configured public name>' -c user.email='<configured noreply address>' commit -m '<accepted merged work>'`, with normal hooks, then `bin/factory push -C <profile-clone> origin HEAD:main`. Never bypass hooks or change global identity/settings.
7. Verify the remote commit and rendered block, then `bin/factory sync mark profile --file <profile-clone>/README.md --ref <commit>`. The CLI checks PR mentions and hashes the matching file, recording mentioned merged PRs still owed and listing missing ones. Add `<pr-url>` after profile to capture only that PR; absent mentions are refused.

## Records and factory commits

Missing lesson records for closed or silent PRs block all new submissions, including another repository's ship dry-run. `next` dispatches those lesson actions before shipping; read the PR's closing or silence evidence and save a real lesson rather than bypassing the submission gate. A previously linked silence lesson does not cover a later close. New closing lessons bind the observed closed_at; legacy lessons qualify only when their source names this PR and is dated after the close day. A same-day legacy source has no time-of-day proof of a closing review.

`bin/factory sync interview --write` generates factual contribution tables and the index. Preserve old narratives. Only a genuinely new insight needs `bin/factory brief record --pr <url>` and a small teaching addition. Do not regenerate every guide for a status change. After reviewing the actual destination content, run `bin/factory sync mark teach --file docs/interview`; directories include .md and .txt files, and each receipt hashes the file that mentions its PR.

Use `bin/factory sync notebook --out <outbox-dir>` once per batch. Read the small status note and sha through the connector, update it, and read it back byte-for-byte, including backslashes. Retain the historical career note; the small derived status is a separate note, not a replacement for that history. Deduplicate events by PR and outcome.

The git path is available only after the owner lists the Private notebook repository in `private_push_repos` (`.factory.local.json`); the outbound guard blocks the push otherwise, and the decision is recorded in `docs/owner-actions.md`. With that entry: add a detached worktree of `origin/main` in a scratch directory (the clone's local `main` may hold other sessions' unpushed commits; leave it alone), edit the note there, commit only that file with the configured public identity and the message `note: <path>`, push with a literal path so the guard sees the target, check that `git rev-parse origin/main:<path>` equals `git hash-object <path>`, then remove the worktree. The connector's `sha` is that blob sha. Without the entry, write the small status note through the connector; never reach the push by a path the guard cannot resolve (L-076).

After either write path, run `bin/factory sync mark notebook --file <read-back note file>` (on the git path, use the verified note in the worktree before removing it). If no write path is available, `bin/factory sync mark notebook <url> --deferred '<actual reason>'` records a retry obligation, not completion; next waits twelve hours before raising it again. --file and --deferred exclude each other; --deferred requires a URL. The CLI computes SHA-256 itself; an empty digest is not completion.

Legacy recorded_at is unverified. `bin/factory sync audit --readme <profile-readme> --interview-dir <interview-dir> --notebook-file <read-back-note>` verifies supplied destinations and reports missing PRs without removing their receipts. After checking that the supplied texts are current, add `--clear-missing` to remove only absent unverified destination receipts so normal sync actions become owed. Unsupplied destinations, verified receipts, deferrals and other ledger fields stay unchanged. Never mark an absent record as complete.

`bin/factory commit -m '<what changed>' --path <item-dir>` stages only the named paths and pushes. Inspect both the working diff and staged diff first; remove unrelated `--path` arguments from the printed action. Unrelated tracked edits stay untouched; unrelated staged edits cause a refusal before staging. Pass each actual changed item directory, never an entire runs/day directory while a wave writes there. Without `--path`, the legacy command stages all tracked changes. The wrapper checks size, secrets and public identity; those checks are not proof that the selected scope is yours.

Keep raw validation logs local in the shift's run directory (`runs/<date>/<shift>/` or the runner's `shift-logs/<name>/`), not in commits. Do not force-add ignored logs or rename them to `.txt`. Commit the command, exit code, concise result and local log path in the existing shift/item record; signed item receipts remain subject to their existing evidence rules.

Repository cards are snapshots, not toolchain authorities. Briefs mark facts older than 14 days, undated cards and invalid/future dates as **Verify first**. Markdown cards use `Latest evidence:` (falling back to `Compiled:`); JSON cards use `latest_evidence`, `compiled` or `date`. Update that date only after rechecking the facts, not after a cosmetic edit. Before setup, read the checkout's own `packageManager`, `engines` and runtime pin files even when a card was checked recently.

## Execution provenance

`bin/factory provenance --item <item> --transcripts <dir>` reads `**/*.jsonl`, not workflow summaries or validation logs. Each session needs a user task that names its role (`brief <role>` or `role=<role>`) and item (`--item <path>`, `item=<path>`, or `role=review Review <path>`), followed by assistant messages with the actual `message.model`. Adjacent role and item field lines belong to the same assignment; shell line continuations are supported. An executed, simple `factory brief` invocation or a Write/Edit of an item output can also establish the assignment. Compound shell commands, reference-only reads, metadata edits and paths mentioned as prior art do not establish one. A scout's successful `factory new` result may identify its new item. The output lists rejected candidates with reasons, the searched layout, directory entries and parsed/matching counts; zero matches leave existing provenance untouched.

Work done by the coordinator itself uses its own original session transcript, with an explicit `role=coordinator-review --item <path>` task segment and the actual assistant model, not a made-up worker identity or a copied summary. A custom checking workflow follows the same transcript contract; the wave script is not required. If the session has no attributable role/item segment or model, keep its transcript and explain the gap in the existing review record rather than inventing provenance. Command receipts prove the checks, not the execution identity or independent review. Supply a directory containing all relevant worker and coordinator transcripts when rebuilding provenance, since a successful collection replaces the item's file.

## Factory test suite

The full suite is required only when the shift changed factory code in `factory/`, `bin/`, `schedule/`, `hooks/` or `tests/`; for record-only shifts, brief, lesson and documentation checks suffice. Run `python3 tests/run_suite.py --jobs 4` in the background with output redirected to a file, and check its saved exit code before calling it successful.

Run `bin/factory brief --check-all`, `bin/factory lessons lint`, `python3 -m unittest discover -s tests -p test_docs.py` and `git diff --check` for record-only shifts. For code changes, run focused tests first, then launch the following block in the runner's background mode from the factory checkout; set `log_dir` to a fresh session scratch directory first:

```bash
python3 tests/run_suite.py --jobs 4 > "$log_dir/suite.log" 2>&1
code=$?
printf '%s\n' "$code" > "$log_dir/suite.exit"
exit "$code"
```

Wait for completion, then read the log and check the saved exit code explicitly:

```bash
read -r code < "$log_dir/suite.exit"
printf 'suite exit=%s\n' "$code"
test "$code" -eq 0
```

An absent exit file means unfinished or interrupted, not success. Do not pipe the test command to `tail`: that hides its exit code. The fast runner uses the same discovery set as `python3 -m unittest discover -s tests`, with each module in its own process; plain discover remains supported for serial diagnosis. A failed module makes the full run fail; inspect and fix the cause rather than substituting a passing focused rerun.

## Measurement and lanes

On Monday in UTC+8, next requests influence, sync retro, a dated docs/retro page and review of lanes.json when no recent page exists. Keep submitted-since and merged-since populations separate. Compare two weeks of observations before reducing a lane whose rank and merges did not move; put depth where accepted work moved fastest. Missing samples are not evidence of stagnation. Never count closed-but-adopted work as merged.

## Compatibility and recovery

- The lock is a lease, not a process supervisor. Renew the current generation before every wave. Another session's raw `lock acquire` after expiry is not prevented, even while the old wave still writes. A still-running wave remains busy: do not start another writer, release the lease or reclaim its clone until it has ended. No forced lease recovery belongs in the normal loop.
- Fix rounds are archived per item and survive restarts. A repeated fix-needed review or an over-budget review means coordinator takeover or park, not another free correction round.
- Repository renames are one-hop entries in repo_renames. If a command still compares an old name and stops, inspect that exact case; do not weaken policy/collision checks or blanket-rewrite history.
- A maintainer's own closed attempt, or their bot's, is a design signal rather than an outside-attempt count. Read what they said on closing and follow that direction; do not call it a swarm solely because those PRs closed.
- Followup uses the latest completed check per workflow; ties or untimed runs remain failing. Unresolved bot threads whose latest response is ours wait for the next external reply rather than generating another reply.
- The outgoing scanner examines merge changes relative to every parent and excludes only proven-public revisions. Never extend those exclusions to a local stacked branch to make a scan pass.
- When merging factory branches, duplicate additions are retained only inside explicitly marked CLI registration blocks and only when both commands are still needed. Resolve every other conflict by hand and run the repository suite before any push.
- The scout and value-review cards own candidate freshness, maintainer invitation, policy and collision checks. At most the configured max_hunts_per_shift are started; no qualifying candidate is a normal result. Explore only active exploration lanes when the depth conditions in lines/oss/policy.md hold, at most once per shift and within the first-merge caps.
- Actual execution provenance comes from transcripts. A worker's claim about its model is not proof. Every release still needs coordinator review regardless of the route.
