# Stacked changes

A maintainer-approved change beyond the source-line cap becomes independently reviewable layers: the contract core first, then follow-ups on their own branches. Never split arbitrarily to disguise one oversized change.

Set upper item.json `stacked_on` to the lower local branch. Measure the upper diff, hygiene and source-only red→green against that layer; still test the public-base combined behavior. Name the dependency and its PR/branch explicitly in pr.md. The coordinator submits the upper PR as draft.

Each layer has its own item, tests, receipts, review and coordinator review. Rebase or a changed lower layer requires fresh verification and head bindings; old green output does not cover the new stack. Keep separate worktrees when multiple layers need checkouts. Do not rebase or overwrite someone else's in-flight checkout.

Before designing a large item, quote the maintainer invitation with URL/date, read all closed attempts and agree the public API/UX. Write a one-page design with alternatives and risks. Keep user docs/examples with the layer changing their contract.
