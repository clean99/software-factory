# Software Factory

🤖 An automated pipeline that finds real problems in open-source projects, fixes
them with regression tests, reviews its own work with an independent agent and
submits pull requests with the evidence attached. It runs unattended twice a day.

This repository is the factory. The pull requests are its output and its evidence;
the engineering is the point.

[Dashboard]({dashboard_url}) · [Direction](docs/direction.md) · [Lessons](lessons/LESSONS.md) · [Method](core/METHOD.md)

## The line

```mermaid
flowchart LR
  S[Scout] --> Q[Qualify] --> V[Value review] --> I[Implement]
  I --> R[Independent review] --> C[Coordinator review] --> P[Publish] --> F[Follow-up]
  R -- fix needed --> I
  F -- review request --> I
```

| Stage | What happens | What stops a candidate |
| --- | --- | --- |
| Scout | Finds a concrete failure in a project the factory works on; checks demand, competing work and the project's contribution rules | Claimed issues, duplicates, projects that ban or restrict AI-generated contributions |
| Qualify | Re-reads live GitHub state just before work starts | Fixed upstream, claimed or superseded meanwhile |
| Value review | A separate agent decides whether the impact justifies a change | No named user who hits it, no real impact. Volume for its own sake is a defect |
| Implement | A minimal fix and a regression test in the project's own style, with red/green proof from recorded runs | - |
| Independent review | An agent that did not write the change reruns the proof and looks for counterexamples | Sent back; a second failed round means takeover or parking, not an endless loop |
| Coordinator review | Reruns the regression on the exact head and checks every public sentence | Head changed since review, evidence missing |
| Publish | Scanned push and PR creation; the description discloses automation | Secrets, local paths, personal data |
| Follow-up | Watches CI, review requests and collisions; records the outcome | - |

## How it is engineered

- **State machine, not a script.** `bin/factory next` reads the factory's state and
  returns the next action. Every stage is a command, and every command leaves a
  receipt (arguments, exit status, commits, output hashes), so the evidence is bound
  to the head that ships.
- **Review independence.** Implementation and review are separate agents with
  separate briefs. Review cannot pass on the author's word; it reruns the proof.
- **Unattended shifts.** A scheduler starts a coordinator session twice a day under
  a lease. It retries transient failures, resumes shifts that were cut short,
  releases leases left by dead runs and writes down anything that needs a person
  instead of stopping.
- **Outbound safety.** Every push, PR body and comment goes through scanned
  wrappers (gitleaks plus privacy rules). This public snapshot is exported from a
  private working repository through a file allowlist and the same scans.
- **Selection.** Depth lanes on projects where contributions are accepted. Scouting
  rotates to the least recently scouted project, empty scouts cool down for three
  days, and a new project is explored only when the depth lanes run dry.
- **Cheaper models for bounded work.** Shifts run on cheaper models. The gates,
  review independence and real test evidence do not change with the model.
- **Learning loop.** Review misses and maintainer corrections are recorded in
  [lessons](lessons/LESSONS.md); recurring ones become checks. A weekly retro reads
  the dashboard, names the bottleneck and records the change it expects to move it.

## Metrics

The [dashboard]({dashboard_url}) tracks only what the factory controls. How long
maintainers take to respond, approve or merge is theirs, so it is not tracked. The
headline is first-round quality: the share of reviewed pull requests approved or
merged with no changes requested and no fix pushed after submission. Output,
defects, filtering, factory speed and factory health sit beside it. A metric
without a reliable source is shown as not tracked, never estimated.

## Openly automated

Since 2026-11-20 every new pull request says in its description that it was
prepared by this pipeline and links here. Projects that ban or restrict
AI-generated contributions are not candidates; where a project asks for a
disclosure in a set format, the factory uses that format.

## Run it

Python 3.12+ and Git; the code uses only the standard library. Install `gh` and
`gitleaks` for publication and leak scans.

```sh
python3 tests/run_suite.py --jobs 4
bin/factory --help
bin/factory brief --check-all
bin/factory metrics --help
```

Before a shift, set your own identity, paths and authentication in the ignored
`.factory.local.json`; `factory.config.json` ships inert example values. Then read
`docs/direction.md`, the role cards under `lines/oss/roles/` and
`schedule/session-shift.md`. The scheduler in `schedule/` is an example integration
and needs a local model CLI.

## What is not here

This is a sanitized snapshot. Operational records, private notes, credentials,
local configuration and the private history are not included.
`public-export.json` lists the included files and test suites, and why the
excluded suites (private records, literal leak fixtures) stay in the private repo.
`bin/factory public export` and `bin/factory public publish --dry-run` rebuild
and check a snapshot; see [{repository}](https://github.com/{repository}).
