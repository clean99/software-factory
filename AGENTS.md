# Contribution factory: agent entry

The factory produces contributions to other people's open-source projects. Maintainers are the customers. A PR that wastes their time is a defect even when CI is green. Quality and trust come before volume.

Since 2026-11-20 the factory itself is the owner's portfolio and its contributions are openly automated: read `docs/direction.md` first; it overrides older guidance where they conflict.

## How work is driven

The coordinator runs `bin/factory shift start --owner <session>`, then `bin/factory next --refresh` and loops `bin/factory next`. Do the printed action, let its evidence close it, then ask again. Ordinary next reads saved observations; refresh explicitly observes GitHub. The loop and lease rules are in `schedule/session-shift.md`.
A worker receives one brief from `bin/factory brief <role>` with its item or repository arguments and follows only that brief, including the upstream reading it names. Workers do not run the shift or publish. Freeze item files after handoff.
Use `bin/factory ack <id> --note '<result and evidence>'` only for actions whose result cannot be observed. It cannot replace review, shipping or destination receipts. End with `bin/factory shift end --owner <session>` after all writers finish.

## Roles

Structured review commands validate schema-2 items; legacy items keep their Markdown gate checks.

| Role | Decides | Produces | Checks its output |
| --- | --- | --- | --- |
| coordinator | Next action, release, takeover or park; personally reviews each head and public text | `coordinator-review.json` or legacy `coordinator-review.md`, release and record receipts | `bin/factory review check --item <item> coordinator`; `bin/factory gate <item> --mode release` |
| scout | Whether a wanted, unclaimed candidate exists | `demand.json`, source notes or a no-candidate result | `bin/factory review check --item <item> demand`; legacy `bin/factory qualify <target>` |
| value-review | Build, propose or drop before implementation; did not propose the item | `value-review.json`, proposal when needed | `bin/factory review check --item <item> value` |
| implement | Smallest complete repair in repository style | Code, tests, receipts, `validation.md`, `verify.md`, `pr.md`, `interview.md` | `bin/factory gate <item> --mode prepare` |
| review | Ship, fix-needed or drop; did not write the change | `review.json` or legacy `review.md`, risk-class probes | `bin/factory review check --item <item> review`; legacy `bin/factory gate <item> --mode review` |
| fix | Which review findings require a bounded correction | Archived review, new commit and fresh evidence | `bin/factory gate <item> --mode prepare` |
| follow | Disposition of live feedback and CI failures, correct reply destination | `follow.json`, reply draft, any repair and fresh evidence | `bin/factory gate <item> --mode prepare`; reply-only work needs text review |
| takeover | Repair, narrow or park repeated or over-budget work | `takeover.json`, preserved rounds, repair or reopening condition | `bin/factory gate <item> --mode prepare` for a repair |
| record | Accepted impact, new insight and records still owed | `record.json`, teaching draft and proposed lesson | `bin/factory sync status` |

The coordinator's review card is `coordinator-review`. A second consecutive fix-needed review, an incomplete review or an over-budget review calls for takeover or park, not another automatic correction round.

## Path of an item

| Stage | Legacy | Schema 2 |
| --- | --- | --- |
| Demand and value | Source notes; preserve the existing item format | `demand.json` → independent `value-review.json` with build |
| Build and proof | Code and `validation.md`, with `redgreen.json` | Code with receipts from `bin/factory run`, plus `redgreen.json` |
| Self-review and PR text | `verify.md` with Who hits it + Impact; `pr.md` | `verify.md` and `pr.md` |
| Independent review | `review.md` | `review.json` |
| Coordinator review | `coordinator-review.md` | `coordinator-review.json` |
| Publish | ship records approval and submits | ship records approval and submits |

`bin/factory new` creates schema-2 items. Do not migrate in-flight legacy items or rewrite their historical proofs. Both paths require coordinator review before release.

## One home per rule

- Code checks: `bin/factory checks` names the enforcers; `bin/factory gate` runs the applicable checks.
- Lessons routed to a role: `bin/factory lessons list --role <role> --repo <owner/repo>`. Nobody is asked to read the lessons file as a whole.
- Repository conventions: the card's Brief section in `lines/oss/repos/`, plus the upstream instructions named in the brief.
- Policy in force: `lines/oss/policy.md`; dated history: `lines/oss/decisions.md`. Do not copy caps into another rulebook.
- Hard rules: `lines/oss/roles/_common.md`, printed in every brief. Role cards and selected craft notes supply the task-specific instructions.

## Publishing

Use only `bin/factory ship`, `bin/factory submit`, `bin/factory open-issue`, `bin/factory propose`, `bin/factory reply`, `bin/factory thread-reply`, `bin/factory gh` and `bin/factory push` for GitHub publication. Factory records are committed directly on `main` with `bin/factory commit -m '<what changed>' --path <path>`, which also pushes; do not create a shift branch. This is a coordinator command, not a worker command.
The release gate adds independent review, coordinator review and approval to preparation checks. Reviews must cover the head; schema-2 reviews also bind PR text and validate proof receipts. Use `bin/factory review stamp --item <item> <part> --head <head>` for schema-2 bindings; issue-first review hashes normalize the item's issue reference to `#ISSUE`. Legacy reviews bind the head, not the PR-text hash. Approval binds the exact head and `pr.md` bytes. Identical-tree-and-message recommits can preserve review bindings, not approval.
Approval uses the owner's standing approval recorded with its quote; see `lines/oss/policy.md`. The coordinator records it with `bin/factory approve <item> --note '<applicable owner quote>'`; ship does this itself. The code checks approval bindings, not whether a human reread each item. Workers never record approval.

| Command | What the code requires |
| --- | --- |
| ship | An unsubmitted OSS item in its owning primary checkout; release checks except issue-dependent checks and approval before issue creation; standing approval, then submit. Linked worktrees allow dry-run only. |
| submit | Release gate before a new PR or an actual follow-up push; publication scans, closed-PR lessons, the long-history unresponsive test and collision checks. An already-current or closed PR returns without a push, not a new release approval. |
| open-issue / propose | Approval of the exact draft and prose/privacy scans. Open-issue also requires a built branch and pre-issue release checks; discussion-first `--before-fix` omits code checks. Propose checks text, not code release. |
| reply / thread-reply | Exact-text approval and scans; by default an open PR at the recorded pushed head. Thread-reply also checks the inline destination. These commands do not run the code release gate. |
| gh / push | Scanned fields, files and outgoing commits. Contribution-branch updates require a matching item and release gate; forced updates need an explicit remote-head lease. Generic contribution writes are refused except reviewed PR body/title edits and state repairs of our ledger-listed, non-merged PRs. Body edits use the approved `pr.md` body without its title, retaining factory-rendered disclosure for PRs created from 2026-11-20; pre-cutoff bodies stay unchanged. Own-repository writes retain content checks. |

Ship and submit dry-runs do not publish, but may fetch and refresh gate, hygiene, policy and qualification caches. They are not a guarantee of no local writes.
Repository policy still applies. Legal agreements, account/settings work and required owner-authored prose go in `docs/owner-actions.md`; notify once and continue other work. A clean scan is not publication authority.
An upstream AI policy that only asks a person to take responsibility for, review or stand behind AI-assisted work is covered by the owner's standing endorsement (2026-11-22). The coordinator reads the live rules and records it per repository with `bin/factory policy <repo> --endorse --note '<why>'`; the command accepts only the reviewed wording in `factory/oss/policy.py`, and the gate then accepts exactly those rule texts. Bans, prior-approval rules, human-written prose and bans on AI attribution still block, and disclosure stays.

## Evidence

Commands that prove something run through `bin/factory run --item <item> --cwd <checkout> -- <command>`. The CLI signs receipts containing argv, cwd, commits, exit status and output hashes. Required-check and head-bound review receipts also need an unchanged head and clean tracked files before and after execution. Cite receipt IDs; nobody types an exit code or fabricates output. Signatures guard against accidental edits, not a hostile process with the local key.
Use `bin/factory receipts --item <item>` to inspect proofs and `bin/factory rerun --item <item> <id>` to repeat them. The coordinator reruns the regression command and applicable review obligations, then adds a counterexample. Read the outputs; an exit match alone does not prove the claim.
Gate modes are prepare, review (the default), and release. Prepare omits independent review, coordinator review and approval; review adds independent review and schema-2 required-check receipts; release adds coordinator proof and approval. Review-mode PASS can mark an item ready, not authorized to publish. PASS means these checks passed, not that every claim is true.

## Records and evaluation

`ledger/prs.json` is authoritative for PR outcomes. Use `bin/factory sync status`, then `bin/factory sync profile`, `bin/factory sync interview` or `bin/factory sync notebook` to derive records. These commands do not publish. After writing and reading back a destination, use `bin/factory sync mark <destination> [<pr-url>] --file <read-back file or directory> [--ref <label>]`; the CLI checks PR mentions and hashes the matching file. Without a URL it captures all mentioned merged PRs still owed and lists missing ones; directories include .md and .txt files. Profile, teach and notebook each need their own receipt. Private notebook may use `<pr-url> --deferred '<reason>'` instead of --file; deferred, unverified or empty-digest receipts remain owed.
Run `bin/factory eval list`, `bin/factory eval bundle`, `bin/factory eval score` and `bin/factory eval report` when a role card or execution route changes; follow `evals/README.md`. This reading replay set informs role assignment, not production qualification. Record actual models from transcripts with `bin/factory provenance`, never from worker self-report.

## Map

- `factory/`, `bin/`: CLI and executable checks; `tests/`: regression tests.
- `core/`: line-independent method; `lines/oss/`: roles, craft, templates, repository cards, policy, `lines/oss/lanes.json` and `lines/oss/risk-classes.json`.
- `lines/product/`: own-project line; `skills/`: task procedures.
- `schedule/`: shift loop and runners; `docs/repairs.md`: conditional repairs.
- `runs/`: item metadata and evidence; `ledger/`: outcomes and destination receipts; `lessons/`: routed learning data; `evals/`: frozen reading cases.
- `docs/`: owner guides, owner actions, retrospectives and teaching material.
