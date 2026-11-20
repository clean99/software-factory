# The method (line-independent)

The factory turns agent effort into changes that other people can trust. Code is cheaper to produce than to review. Verification and a useful change matter more than output volume.

The CLI drives; the model executes. Each rule has one home. Evidence is captured, not typed. Value and correctness are judged separately from authorship. Records are derived from outcomes, not remembered by a session.
The OSS line in `lines/oss/` applies this method to contributions. Other lines need their own demand, review and release contracts; sharing a CLI does not establish that those contracts work.

## 1. Roles

Separate authorship from independent review. Decide whether the work is wanted before paying to build it.

| Role | Does | Must not |
| --- | --- | --- |
| Scout | Finds a supported user problem, its source and its contract | Treat a constructed failure as demand |
| Value reviewer | Independently chooses build, propose or drop before implementation | Approve its own proposal |
| Implementer | Reproduces, builds, verifies and drafts public text | Supply the independent review |
| Reviewer | Probes the change against each applicable risk class | Edit the production fix or copy self-review |
| Fix / follow | Repairs bounded findings or handles current external feedback | Erase prior findings or publish |
| Coordinator / takeover | Drives the next action, reruns proofs, decides release or park | Delegate away the final judgment |
| Record | Derives facts and identifies new lessons and teaching needs | Count a draft as a completed destination write |

`bin/factory brief <role>` supplies one role card, hard rules, item facts, repository conventions, selected craft notes and routed lessons. The worker follows that brief, including the upstream reading it names.

## 2. State

An item directory holds `item.json`, evidence and reviews. The ledger holds external outcomes; it wins when an item's old status differs. Pending work is a projection, not another stored to-do list.
The coordinator starts with `bin/factory shift start --owner <session>`, refreshes observations explicitly, then loops `bin/factory next`. Each action names its commands and done-check. A fresh call derives the next action from current files, receipts and saved external observations.
Observable completion needs evidence. `bin/factory ack <id> --note '<result and evidence>'` records only results the CLI cannot observe; changed inputs invalidate the acknowledgement. A lease coordinates writers, and the coordinator waits for them before `bin/factory shift end --owner <session>`.
New OSS items use schema 2. Legacy items keep their Markdown proofs and review format, with coordinator review required on release. Migration must not rewrite old evidence into claims it never established.

## 3. The verification ladder

Each rung catches something the previous one cannot. This is a way to reason about evidence, not a claim that the gate enforces every rung for every item.

| Rung | Question | Mechanism |
| --- | --- | --- |
| V0 demand | Who hits it, and should it change? | Sourced demand, contract and independent pre-build value review |
| V1 static | Does it meet the repository's mechanical rules? | Lint, format, typecheck and build receipts captured by `bin/factory run` |
| V2 behaviour | Does the test distinguish the fix from the old code? | Captured test receipts and `bin/factory redgreen`, which retracts source changes while keeping tests |
| V3 runtime | Does the changed contract work in the running product? | Captured runtime/e2e commands and real demos when the contract needs them |
| V4 independent judgment | What did the author miss? | Independent review, applicable risk-class probes and head-bound receipts |
| V5 release judgment | Does the coordinator stand behind this version? | Read diff, tests, demand and public text; rerun proofs, add a counterexample, check approval |
| V6 the world | What did maintainers and their CI do? | `bin/factory followup`, ledger outcomes and destination receipts |

A receipt records argv, cwd, commits, output hash and exit status, signed by the CLI. Required-check and head-bound review receipts need the same head and clean tracked files before and after execution. Agents cite receipts; they never type exit or comparison fields. `bin/factory rerun` repeats the recorded command and computes the comparison.
A failing command is not necessarily a failing regression assertion. A green suite is not coverage of every changed contract. Reviewers inspect outputs, assertion strength, readers, writers and real input paths. Runtime evidence is required by the task, not by a universal screenshot quota.

## 4. Gates

- `bin/factory gate <item> --mode prepare` checks preparation without independent review, coordinator review or approval.
- `bin/factory gate <item> --mode review` adds independent review and schema-2 required-check receipts. This is the default mode.
- `bin/factory gate <item> --mode release` adds coordinator review and approval; schema 2 also checks coordinator reruns and a distinct counterexample.
- Review bindings cover the head; schema-2 reviews also cover PR text. `bin/factory review stamp` writes schema-2 bindings and normalizes the item's issue-first reference to `#ISSUE`. Approval still binds the exact head and exact `pr.md` bytes. Identical-tree-and-message recommits can preserve review evidence, not approval. Other content changes require fresh bindings.
- The gate recomputes mechanical checks and validates recorded evidence. It does not rerun every proof or establish the truth of a source quotation. The coordinator still reads the sources and outputs.
- Failed checks name the missing evidence or repair. Fix the cause or record a blocker; never lower a check to obtain PASS. Release commands rerun their applicable checks rather than trusting an item status.

The shared gate is `factory/core/gate.py`; `bin/factory checks` names its enforcers and line-specific checks. An approval record can cite standing owner authority where line policy permits it. It is not proof of a new human review.

## 5. Stopping

A worker hands off completed evidence or saves partial work and a precise blocker before its role budget expires. Waiting on an external party means stop, not sleep-poll.
Fix rounds survive restarts in archived review files. A second consecutive fix-needed review, an incomplete review or an over-budget review calls for coordinator takeover or park. Starting a new context does not reset the work's history.
`bin/factory stop-check` checks ready claims and lessons owed; it is not a complete scheduler or proof that all preparing work finished. The next-action loop owns the remaining work. Failure must be reported, not silently abandoned.

## 6. Lessons

- Record useful external rejections and maintainer feedback with source, scope, status and enforcement. Preserve the failed attempt rather than rewriting its story.
- Give each rule an operational home: a code check, the relevant role's brief, a repository card or a named procedure. Retire rules that no longer apply. Nobody reads the lessons file as a whole before working.
- `bin/factory lessons list --role <role> --repo <owner/repo>` selects relevant instructions; `bin/factory lessons lint` checks routing. A routing label does not prove that the worker applied the rule.
- Candidate observations need confirmation before becoming general rules. Repeated failures call for a better check or narrower scope, not another copy of the same warning.
- Derive counts and status tables from the ledger with `bin/factory sync`. Each destination needs a separate receipt after a real write and read-back. Deferred or unverified records remain owed; teaching prose does not delay a ready profile update.

## 7. Models

Assign roles from what the replay set shows, not a strong/weak label. Run `bin/factory eval` under the protocol in `evals/README.md` when a role card or execution route changes. Keep related cases together and include accepted controls so blanket rejection cannot pass.
The set checks reading judgments, not implementation or tool discipline. Critical verdict scoring still needs manual grading of reasoning. Report false ships, false drops, unfinished work, wrong verdicts and elapsed time without turning a selected replay set into a production guarantee.
Read the actual model from transcript `message.model` fields with `bin/factory provenance`, not worker self-report or requested routing metadata. Preserve attempts and routing differences.
Author and reviewer remain separate. The OSS gate permits a declared same-model fresh context only for small items, with a warning; large items need a different model. Those string checks are not model authentication.

## 8. Adding a line

Create a directory under `lines/` with a policy, role cards, selected craft notes, templates and a PLAYBOOK index. Define its demand evidence, state observations, review obligations, release authority and destination records before automating it.
Add line checks and CLI registration under `factory/`, and connect them to gate modes and next-action derivation. Test both refusals and accepted controls. Keep reusable evidence, review and ledger mechanisms in `factory/core/`.
For an application line, this means sourced requirements, testable acceptance criteria, code and runtime evidence, independent review, authorized release with rollback, and measured outcomes. It is a design task, not an already demonstrated deployment line.
