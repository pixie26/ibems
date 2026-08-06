# ADR-002 — cancel-confirm-reconcile-new, not in-place modification

**Status:** Accepted · **Date:** 2026-08-06 · **Revisit:** before any material size increase

## Context

To change a working order's price we can either modify in place (same orderId,
atomic broker-side) or cancel and submit a new one.

In-place modification is atomic at the broker but introduces an ambiguity we
cannot resolve locally: did the modification land before or after a fill? Order
identity also becomes mutable, which complicates attribution across restarts.

Cancel-then-new has one genuine cost: a **zero-order window** between cancel
confirmation and the new order, during which the market can move away.

## Decision

**cancel → await terminal → reconcile → new.**

Explicitly accepted at 1–5 shares: the zero-order window is real and we are
choosing provability over fill quality.

## Consequences

- Order identity is immutable; every attempt is a distinct intent.
- Fault injection is straightforward; the interleavings are enumerable.
- Fill quality is worse in fast markets — precisely where this strategy's P&L
  lives.

**Revisit trigger:** any material size increase. Measure the zero-order window
cost from Gate D data before deciding. Do not carry this choice forward by
inertia.
