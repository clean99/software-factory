You are running an unattended shift in the contribution factory. Nobody is watching; nothing you do may need a human in the loop to be safe.

1. Read `AGENTS.md`. Run `bin/factory doctor`, `bin/factory followup --notify`, `bin/factory status`.
2. For every open PR with review comments or failing CI: diagnose, fix on the same branch if the fix is clear, re-run redgreen and gate, and draft `reply.md`. Do not post anything.
3. For every closed PR without a lesson: read the closing comments and record the lesson with `bin/factory lessons add` (it asks for the stage and where the rule is enforced).
4. If fewer than three items are `ready` or in progress: pick the first active repository printed by `bin/factory lanes`, follow `bin/factory brief scout --repo <repo>` and then `bin/factory brief implement --item <item>` for at most one new item.
5. Never run `approve`, `approve-proposal`, `approve-reply`, `submit`, `propose`, `reply` or `thread-reply`: those need the owner.
6. Finish with `runs/<today>/shift-report.md`: what changed, what needs the owner, what is blocked and why. Its last line is a one-sentence summary under 120 characters.
