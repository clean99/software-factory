# Metrics

What the factory counts about its own pull requests, how each number is computed, and which part of the factory is expected to act on it. The status board, linked at the top of the [README](../README.md), shows current values with their date and sample size. This page is the contract behind them.

## What is counted

Every pull request the factory opened in a repository its account does not own, from the moment it was created. Drafts, withdrawn pull requests, closed pull requests and ones nobody answered all stay in.

The list comes from the factory's ledger. Each time the saved review history is refreshed, the list is compared with GitHub's own search for the account's pull requests, and the dashboard shows any difference. On 2026-11-20 both listed the same 129, and none of the ledger's 154 committed revisions held a pull request that was later removed.

## Daily activity

Daily activity is separate from the UTC-date cohort below. It groups events by their `Asia/Singapore` date, using `zoneinfo`, over 28 local dates ending on the last successfully observed date. Empty dates have zero counts. Today is always a partial day, not a completed-day total.

- Opened uses GitHub's creation time (`created_at`, called `createdAt` in GraphQL). An exact ledger `submitted_at` is a fallback when no creation time is saved; it is not assumed to equal GitHub's time. The fallback is saved with the observation so a failed refresh cannot move the opening date.
- Merged uses `merged_at` (`mergedAt`). Closed without merge uses the final `closed_at` (`closedAt`) of a currently closed, unmerged pull request. A merge never also counts as a closure.
- A reopened pull request that is open now has no terminal event. If it later closes or merges, it contributes one terminal event at that final time. `reopened` counts pull requests with at least one recorded reopen, not the number of reopen events.
- A cited replacement pair in `ledger/pr-outcomes.json` counts as one contribution: the new pull request adds no opening, and the old one adds no closure. Both pull requests still count separately in the cohort rates.

Lifetime totals include events outside the window and events without a timestamp. `missing` counts activity events whose exact times are absent; they are omitted from the daily rows. A submission time or check time never stands in for a merge or closure time. The first opening date and number of projects with merges accompany the totals. Reconciliation checks `opened = merged + closed_unmerged + open` and exposes any difference, unclassified state, or invalid replacement rather than assigning it to open.

Activity includes only public pull requests in the ledger. Search is restricted to public repositories; extra search results appear in the population difference but do not enter daily counts or trigger timeline reads. The search starts at the earliest saved creation date, or one UTC day before submission when creation is absent. Missing ledger URLs are checked directly for public visibility and authorship before admission; the search difference remains visible.

`as_of` and `as_of_local_date` identify the successful GitHub observation behind the counts, not the time the page was generated. Reconciliation reads every search page and every needed timeline page. An incomplete search, a search beyond GitHub's 1,000-result ceiling, an unresolved ledger pull request, or an API error fails the check. A failed refresh keeps the last good counts and date window, sets `observation.ok` to false, and reports the error with the last successful check time. Saved snapshots are checked for invalid rows, duplicate URLs, and population differences before use. Damaged snapshots are rejected; ledger or history evidence is shown with a failed observation rather than as a successful empty population. Without a successful activity check, saved evidence is shown as not checked; absent observation times stay null.

The work queue asks for a complete refresh at the close of a session whenever the last attempt is more than twelve hours old, so with three daily shifts the history is refreshed at most once in any twelve hours, by the first shift that closes after that. A failed attempt is saved with its time and counts as an attempt; the last good counts stay on the page until a later refresh succeeds.

`metrics history --limit N` fetches at most N PR histories and defers population and activity reconciliation, leaving its last snapshot unchanged. Run without `--limit` for a complete refresh. `--no-reconcile` also skips population and activity acquisition.

On 2026-11-21, 97 of 129 ledger submission times were one to three seconds later than GitHub's creation times; none changed the UTC or Singapore date. GitHub supplied the nine closure times absent from the ledger. Four pull requests had reopened and later merged. The replacement check found no same-branch or shared-issue pair among the 15 same-repository pairs where an old closure preceded a new opening.

Merge rate is decided by maintainers. Daily counts do not change its denominator, and waiting time is not a KPI.

## Cohort

Rates use only pull requests opened at least 14 days before the snapshot. Membership goes by UTC date: a pull request is in when its opened date is on or before the snapshot date minus 14 days. That makes the cohort reproducible with a GitHub search, and the dashboard links the queries.

- A newer pull request has had little time to be judged. Counting it as "not merged" understates the rate; leaving it out of a "merged or closed" denominator overstates it. A fixed cutoff reduces both distortions. It does not remove them: on 2026-11-20, 19 pull requests past the cutoff were still open, and their outcome can still change the numbers.
- Why 14: among the 100 pull requests merged by 2026-11-20, the slowest took 11.2 days (median 1.3, 90th percentile 5.6). That describes merges that happened. It is not a claim about how long a maintainer should take.
- The window is a constant in `factory/core/metrics.py`. If it changes, the dashboard carries a dated note.
- A pull request still open after the cutoff stays in the merge-rate denominator. If it merges later, it moves to merged. The rate is therefore "merged so far, among pull requests at least 14 days old". It is not "merged within 14 days", and it is not a forecast of the final share.
- No rate is shown when its own denominator is below 20. Counts are shown instead.

## The two rates

| Rate | Numerator | Denominator | Who decides it | What the factory can change |
| --- | --- | --- | --- | --- |
| Merge rate | Cohort pull requests that merged | Every cohort pull request, including closed and unanswered ones | Maintainers | What it chooses to work on, and where |
| Merged without rework | Cohort merges with no rework after opening | Cohort merges with a complete saved history | The factory's build and review, and how strictly each project reviews | What it builds and what its own review catches |

The second rate is conditional on a merge. It says nothing about pull requests that were closed or are still open; those are in the merge rate. Withdrawing an open pull request changes neither rate on the day: it was already in the first denominator and outside the second. Compared with reworking it until it merges, withdrawing leaves the merge rate lower and the second rate higher, which is why the second rate is never shown without the first. Showing both makes that selection effect visible; it does not remove it.

Merge rate is shown with a 95% Wilson interval. The interval treats pull requests as independent, which they are not: on 2026-11-20, 63 of 100 merges came from five projects. The dashboard shows the split by project for that reason.

One more share is recorded as "merged among decided": merged / (merged + closed) over all pull requests. It was 90.9% on 2026-11-20. It leaves out every pull request that is still open, so on the same data it reads higher than the merge rate.

### Merged without rework

A merge counts as reworked when any of these happened between opening and merging:

1. A reviewer formally requested changes.
2. The factory pushed a content change. Merge-from-base commits do not count.
3. A maintainer repaired the submitted change or replaced its approach on the branch.

A merge whose saved history is incomplete is left out of this rate and shown as unverified.

Maintainer edits are classified per pull request, after the merge, when the set of commits is final. The class is the strongest thing a maintainer did to the submitted change, with one commit linked as evidence: `repair`, `scope`, `refactor`, `style`, `tests`, `notes`, `bot` or `base`. Only `repair` counts as rework. A repair to something the maintainer's own earlier commit introduced is not a repair of the submission. A pull request with maintainer commits and no class counts as reworked until someone classifies it.

On 2026-11-20, 25 merged pull requests carried maintainer commits. In 7 the maintainer repaired the submission or replaced its approach. In the other 18 the edits were tests, release notes, refactors, extensions or bot output.

## Reasons

Each cohort pull request that was not merged without rework has one recorded reason. Most reasons name the stage that could have prevented the outcome. A pull request in review and a closure nobody explained name no stage, and one waiting on an agreement waits on the owner.

| Outcome | Reason | Established by | Stage that owns it |
| --- | --- | --- | --- |
| Merged after rework | A maintainer formally requested changes | A `CHANGES_REQUESTED` review from another account | Independent review |
| Merged after rework | The factory changed it after opening | A non-merge commit or force-push by the author after creation | Required checks, independent review |
| Merged after rework | A maintainer repaired it | A pull request classified `repair` | Build, independent review |
| Closed | No real-world case shown | The maintainer's closing comment | Value review |
| Closed | Works as designed, or approach declined | The maintainer's closing comment | Value review |
| Closed | Someone else fixed it first | A linked pull request | Qualify |
| Closed | The maintainer fixed it another way | The maintainer's closing comment and the replacing change | Build: fix the layer that owns the contract |
| Closed | Project rule not followed, withdrawn, or no reason given | The closing event | Repository card, or none |
| Open | No reply from a person | Ledger state: no maintainer or community response recorded | Project selection |
| Open | In review, or waiting on an agreement | Ledger state | None, or the owner |
| Open | Not checked yet | Ledger state: follow-up has not read the thread | Follow-up |

When several rework reasons apply, the first in the table is recorded. Closure reasons and maintainer-edit classes are kept in `ledger/pr-outcomes.json`, each with a public link. An entry without a link is not accepted.

## Process numbers

These are counted inside the factory and do not depend on maintainers: candidates recorded, candidates stopped before publishing, candidates sent back by independent review, automated sessions that exited cleanly, and the factory's own test count. They explain the two rates. They are not targets, and none of them is a headline.

## Not measured

| Number | Why |
| --- | --- |
| Time for maintainers to answer, approve or merge | It is their time. The factory cannot change it and should not be tuned to chase it |
| Cost per pull request | Worker sessions are not attributed to items yet |
| Human interventions per session | Sessions carry no intervention record yet |
| Reverts and regressions after merge | Follow-up stops when a pull request merges |

## Known limits

- GitHub does not publish the time of an ordinary push. Commit dates stand in for it. In the 2026-11-20 audit, four force-push events showed a head that was committed before the pull request opened and pushed after. Force-push times are exact.
- A force-push that rewrites commit metadata without changing content still counts as a content change.
- Every merge commit is treated as a merge from the base branch. A fix made inside a merge commit would be missed. None was found in the 16 merge commits audited on 2026-11-20.
- A ledger row with only a date and no saved history is left out of the cohort and counted as undated. There were none on 2026-11-20.
- Saved review history records whether a reviewer was the author or someone else, not whether it was a person or a bot. In the same audit 83 of 184 formal reviews came from bots, and 31 of 100 merges had no formal review from a person. The two rates do not depend on that distinction. The "no reply from a person" reason does, and is taken from the ledger.
- "Merged without rework" counts recorded fixes. It does not measure a maintainer's effort: on 2026-11-20, 16 cohort merges carried maintainer edits that were not repairs, and the same test added by the factory after opening would have counted as rework.
- Closure reasons and maintainer-edit classes are judgments. An agent reads the public thread or commit, the coordinator reviews the entry, and the link is published so anyone can disagree. Of the first ten closures, one had no stated reason and is recorded as such.
- Four weeks of history cannot show a trend. Weekly values are published as counts.
- Replacement links require a recorded source. Unlinked pairs still count as two in daily activity; the cohort always counts pull requests separately.

## Rules against flattering the numbers

- Closed, withdrawn and unanswered pull requests stay in the merge-rate denominator and in the pull-request outcome counts. Daily activity combines only recorded replacement pairs.
- A merge with unclassified maintainer edits counts as reworked, and a closure with no recorded reason is shown as unclassified, until someone classifies it.
- Merged counts are published as evidence, next to the size of the cohort they came from. They are not a headline. An early target of ten merges a day was dropped on 2026-11-21. The owner's current target, set on 2026-11-22, is ten new pull requests in every contribution shift: it counts pull requests opened, not merged, it never relaxes the quality gates, and a shift short of it is reported with its count and reason.
- The window, the minimum sample and the formulas live in code and on this page. A change to any of them is dated on the dashboard.
