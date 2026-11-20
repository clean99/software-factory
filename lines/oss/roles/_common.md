# Worker rules

- No outward actions: no push, PR, issue, comment, reaction, publish or account changes. Never approve for the owner. Plain `gh` is read-only.
- Allowed factory commands only: brief, doctor, status, new, qualify, policy, health, lanes, plan, run, rerun, receipts, redgreen, slop, gate, review, lessons list, sync status. The rest are the coordinator's.
- Commit only in the assigned contribution clone, on its item branch: `git commit`. Use the clone's configured public identity, not an example. Never stage, commit or switch the factory; pushed rewrites need the follow exception.
- Never --no-verify, -n, config/hook/security/credential changes or hook installers. If `git config --local --get core.hooksPath` prints a value in the clone, stop and report it.
- Open automation: ship discloses new PRs from 2026-11-20 using upstream formats; never claim human review. Skip AI bans/restrictions. No commit trailers. Leave old PRs/replies unchanged unless maintainers ask. See `docs/direction.md`.
- Before installs/builds/tests require at least 6 GiB free (`df -g ~`). Use CI=1 HUSKY=0 LEFTHOOK=0; Python installs UV_NO_CACHE=1; public registry https://registry.npmjs.org; Node 24.16.0 unless pinned otherwise. Use dependencies in this worktree, never borrowed ones.
- No personal data, private image query strings or files over 2 MiB in git. Do not kill others' processes, spawn agents or invoke skills. Never delete tracked fixtures or others' dependencies.
- Evidence comes from factory run receipts, never typed. Grade claims A=reproduced, B=code, C=inference. Re-read live GitHub state before claiming it.
- Fill the item's .md files in place; they are deliverables.
- Freeze item files after handoff; never sleep-poll.
