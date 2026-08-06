# ADR-003 — No MOC or auction orders in V1

**Status:** Accepted · **Date:** 2026-08-06

## Context

MOC is the natural instrument for an EOD-flat strategy. Two problems:

1. **Irrevocability.** Past the exchange cutoff an MOC cannot be cancelled. The
   platform's core abstraction is a *mutable* target position. Once an MOC is
   live, the target is no longer mutable — a contradiction requiring a dedicated
   `LOCKED_AUCTION` state that rejects all subsequent targets.
2. **Unvalidatable.** IB paper does not support auction orders, so Gate B2 could
   not exercise the path at all. Shipping code whose first execution is in
   production is not acceptable for this component.

## Decision

V1 supports `MARKETABLE_LIMIT` and `MARKET` only.

EOD flatten: begin at close − 15 min with a marketable limit, widen the collar
by rung, use a more aggressive final order before the hard deadline.

## Consequences

- Worse EOD execution than an MOC would give. Accepted; quantify from Gate D.
- Adding MOC later requires `CLOSE_COMMITTED` / `LOCKED_AUCTION` states plus a
  documented recovery path when a closing execution never arrives.
- Half-day sessions move the cutoff too — another reason not to take this on now.
