# Direction: the factory is the portfolio

Decided by the owner on 2026-11-20. This page overrides older guidance where they conflict.

## What changes

The product is the factory, not the individual pull requests. The project demonstrates that its owner can engineer a system that ships useful code to real open-source projects, fast and at a high bar. Pull requests are the factory's output and its evidence.

The factory stops presenting its work as hand-written. Contributions are openly automated:

- Every new pull request says in its description that it was prepared by this automated pipeline and links the public repository. Commits keep each project's own conventions; the disclosure lives in the description, where reviewers read it.
- A project whose rules ban or restrict AI-generated contributions is not a candidate. Where a project asks for a disclosure in a set format, the factory uses that format.
- Pull requests opened before 2026-11-20 are left as they are.
- The GitHub profile says plainly that the account is automated, shows the merged count and links the dashboard and the factory.

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
| Dashboard | A static page built from the metrics, published with the public repository and refreshed after each shift. |
| GitHub profile | An automated badge, the merged count, links to the dashboard and the factory; the factory repository is pinned first. |

## Metrics

Track only what the factory controls. How long maintainers take to answer, approve or merge depends on them, so those durations and the share of silent threads are not tracked. Whether a pull request is good enough to be approved in the first round is ours, and it is the headline quality metric.

Each metric is computed from records the factory already keeps (ledger, item records, receipts, shift summaries). A metric without a reliable source is shown as not tracked rather than estimated.

| Group | Metrics |
| --- | --- |
| Output | Pull requests opened and merged; per week |
| First-round quality | Share of reviewed pull requests approved or merged with no changes requested and no fix pushed after submission (headline); review rounds per pull request |
| Defects | CI failures after submission; fixes the factory had to push after review; reverts and regressions reported after merge; pull requests closed as duplicate, already fixed or unwanted (a candidate-selection miss) |
| Filtering | Candidates found, dropped at qualify and value review, and sent back by independent review |
| Factory speed | Lead time from candidate found to pull request opened; time from a reviewer's request to the factory's pushed fix |
| Efficiency | Agent minutes and tool calls per pull request and per merged pull request; shifts run unattended; owner interventions per week |
| Factory health | Shift completion rate; factory test count and runtime; factory defects found and fixed |

## Working rule

A change to the factory is judged by its effect on these metrics. Each weekly retro reads the dashboard, names the bottleneck, and records the change it makes and the metric it expects to move.
