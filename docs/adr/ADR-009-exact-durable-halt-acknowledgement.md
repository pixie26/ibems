# ADR-009 — HALT acknowledgement is an exact durable compare-and-set

## Decision

An acknowledgement references the exact latest active HALT cause. The journal
performs the check and append in one transaction. A stale token fails. A new cause
while already HALTED creates a new durable cause and advances the token.

Acknowledgement does not resume the current process. Manual stop, account review,
restart and stable reconciliation are still required.

## Why

Without exact binding, an operator viewing an old incident can clear a newer one.
Automatically resuming after acknowledgement also hides whether the triggering
fault has actually been removed.
