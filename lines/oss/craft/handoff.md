## Output contract

Return only {item, head, verdict, summary}; review roles also return complete (boolean). Scout/value-review verdicts: build|propose|drop; other roles: ship|fix-needed|drop. An unfinished review uses complete=false and omits verdict.

item: directory (empty only if no candidate); head: full live SHA (empty only before branching). summary: artifact paths, checks left, blockers; no raw logs. Ship means ready for the next review, never released.

Validate output with: the role card's Check command.
