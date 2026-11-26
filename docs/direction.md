# Direction: the factory is the portfolio

Decided by the owner on 2026-11-20. This page overrides older guidance where they conflict.

## What changes

The product is the factory, not the individual pull requests. The project demonstrates that its owner can engineer a system that ships useful code to real open-source projects, fast and at a high bar. Pull requests are the factory's output and its evidence.

The factory stops presenting its work as hand-written. Contributions are openly automated:

- Every new pull request says in its description that it was prepared by this automated pipeline and links the public repository. Commits keep each project's own conventions; the disclosure lives in the description, where reviewers read it.
- A project whose rules ban or restrict AI-generated contributions is not a candidate. Where a project asks for a disclosure in a set format, the factory uses that format.
- Pull requests opened before 2026-11-20 are left as they are.
- The GitHub profile says plainly that the account is automated, shows the generated status card and daily activity, and links the factory's home page, source and status board.

The owner talks about how the factory is engineered: architecture, gates, review independence, evaluation, cost, running on cheaper models, failure modes and their fixes. Individual pull requests are evidence, not the story.

## What stays

- Only pull requests with a real positive impact. Each names who hits the problem and the impact, with evidence. Volume for its own sake is a defect.
- No issue that its reporter or anyone else has claimed.
- Independent review and coordinator review before anything is published.
- Personal data stays private: account details, local paths, employer, private notes, owner actions and interview material are never published.

## Public surfaces

| Surface | Content |
| --- | --- |
| Public factory repository | The factory's code, tests, schedules, docs, lessons and metrics, exported from this private repository after a leak scan. Records that carry personal data stay here. |
| Home page | The landing page at `public.dashboard_url`, explaining the factory through its process, outcomes and evidence. |
| Status board | `board.html` beside the landing page, built from the metrics and published with the public repository. The public snapshot is refreshed after daily sessions, not from unreviewed weekly changes. |
| GitHub profile | A plain statement that the account's pull requests are automated, a generated text status card, daily activity and links to the home page, source and status board. The factory is the only pinned repository. |

## Metrics

Two rates lead. Both are defined in `docs/metrics.md` and use one cohort: pull requests opened at least 14 days before the snapshot.

| Rate | Formula | Who decides it |
| --- | --- | --- |
| Merge rate | merged / opened, with closed and unanswered pull requests kept in the denominator | Maintainers. The factory influences it through what it selects. It is kept as the signal of whether projects want the work, and it is never presented as something the factory controls |
| First-round quality | merged without rework / merged | The factory's build and review, and how strictly each project reviews. This is the headline quality number |

How long maintainers take to answer, approve or merge is their time. Those durations are not tracked and are never a target. A pull request nobody answered is counted under its own reason; it is not timed.

Every pull request that was not merged without rework gets one recorded reason, and most reasons name the stage that owns them. Process numbers such as candidates stopped before publishing, send-backs by independent review, scheduled sessions finished and the factory's test count explain the two rates. They are not goals. A number without a reliable source is shown as not measured rather than estimated.

## Working rule

A change to the factory is judged by its effect on these metrics. The checked-in schedule starts three daily coordinator sessions, at 05:43, 13:43 and 21:43 Asia/Singapore, and a weekly review on Sunday at 22:30. Since 2026-11-22 the owner sets a target of ten new pull requests in every contribution shift; the quality rules above still decide each one, and a shortfall is reported rather than filled. The weekly review reads the status board, names the bottleneck and prepares changes for the owner to accept. Proposed changes to `factory/core/public-improvements.json` stay on the review branch until accepted; the public snapshot must not present them as deployed.
