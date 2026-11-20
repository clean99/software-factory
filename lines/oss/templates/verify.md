# Self-review as the strictest maintainer of <repo>

Who hits it: <open report or discussion, captured output of a real tool/API, or documented usage the repo supports (L-061)>
Impact: <what users see change once this is merged, one sentence>

## Reasons to reject
- [ ] every changed line serves the issue
- [ ] naming, imports, error handling and file layout match the neighbours
- [ ] no comments that restate code; no new comments the file would not have
- [ ] the test fails without the fix and asserts the changed behaviour
- [ ] edge cases a reviewer will ask about are handled or consciously out of scope
- [ ] PR text is short, specific, and reads like the author wrote it
- [ ] no collision: issue still free, no similar open PR

## Findings
<each problem found and how it was fixed>

verdict: <ship | fix-needed | drop>
