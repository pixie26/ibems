# ADR-001 — Exclusive account ownership

**Status:** Accepted · **Date:** 2026-08-06

## Context

`ib.positions()` returns **account-level** aggregate positions. A target-position
OMS that treats that number as "its" position will happily "correct" a position
it did not create — including one belonging to a second strategy or a manual
trade.

Two options:

- **A. Exclusive account.** One account, one strategy, one platform. Anything
  unattributable → HALT.
- **B. Strategy shadow positions.** Rebuild per-strategy position from the
  `orderRef`-tagged execution stream; account position becomes a checksum only.

## Decision

**A.** Tolerance is **0 shares**, not a threshold.

Rules: no manual TWS orders in this account · no second strategy · no
pre-existing position at boot · anything the journal cannot explain → HALT, never
auto-adopt, never ignore.

## Consequences

B is roughly an order of magnitude more state. V1 does not pay that cost for a
capability it does not need. If a second strategy ever shares an account, this
ADR is superseded and the whole reconciliation path is rewritten — not patched.

Detection uses `reqAllOpenOrders()` (reads without binding) rather than a
clientId-0 subscription, because binding can trigger cancel/resubmit and cost
queue position on an order we did not place.
