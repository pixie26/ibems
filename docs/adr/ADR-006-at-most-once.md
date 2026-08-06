# ADR-006 — At-most-once submission

**Status:** Accepted · **Date:** 2026-08-06

## Context

There is no atomic transaction spanning our database and the broker. Some crash
window always leaves delivery unknown. We must choose which error we prefer.

- **At-least-once:** never miss a trade; may duplicate.
- **At-most-once:** never duplicate; may miss.

## Decision

**At-most-once.** When we cannot know whether an order was delivered, we do not
send another. Resolution is reconciliation, never retry.

Write ordering is fixed (SPEC §5): claim `decision_id` → commit intent → commit
`SEND_ATTEMPT_STARTED` → **then** call the broker.

## Consequences

- A duplicate produces a position nobody sized for and may be unwound at a loss
  in a market that already moved. A miss costs one opportunity in a strategy
  whose P&L is concentrated in a few days a year.
- Cost: misses are now a real term in the cost model, tracked as
  `DECISION_MISSED` (SPEC §10.1). This term does not exist in the backtest at
  all, and for straddle-shaped P&L it may dominate slippage.
- The asymmetry is size-dependent, like invariant 19. At V1 size a miss is
  nearly free. Revisit if size grows.
