# Issue first

Distinguish “an issue before the PR” from “discussion/assignment before coding”. Read the actual contribution rule and all comments. A feature, public API/UX change or uncertain scope needs agreement to the proposed shape; a general “PR welcome” is not design approval.

For discussion-first work, draft proposal.md in about ten lines: plan, API/UX sketch, scope, test plan and one concrete question. No response after 14 days means blocked and move on, not repeated pings. A private prototype never waives agreement before release.

For a mechanical issue-link requirement, prepare the fix, reproduction, lint/typecheck/tests/build, repository hook commands and gate first. Draft issue.md in the bug template with a runnable reproduction; put exactly `resolves #ISSUE` in pr.md. The coordinator opens the issue and submits the ready branch back to back, then verifies the live links/head. Do not file early just to reserve the issue.

Keep legal agreements, human-only prose and maintainer acceptance separate. A scanner warning is evidence to inspect, not permission to declare the owner has acted. Hand the source and exact remaining action to the coordinator.
