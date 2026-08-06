# ADR-005 — The recorder must not be able to harm the trading path

**Status:** Accepted · **Date:** 2026-08-06

## Context

The quote recorder runs continuously from Week 0 and shares infrastructure with
the execution engine. The risk is directional: a recorder crash-looping its
subscriptions can trip IB's ~50 msg/sec pacing limit and get the **Gateway**
disconnected — taking the execution engine down with it.

The recorder is the lower-value process. It must not be able to damage the
higher-value one.

## Decision

Preferred: **separate Gateway instance** (own paper login, own process).

Minimum acceptable: same Gateway, but the recorder gets its own token bucket,
bounded exponential backoff with a maximum retry count, its own clientId (33),
and `readonly=True` on the API connection so it is structurally incapable of
placing an order.

## Consequences

- Extra process to operate. Accepted.
- Recorder reliability is subordinate to trading-path reliability: a recorder
  that gives up and alerts is correct; one that retries aggressively is not.
- Daily health report is mandatory (RUNBOOK §2). The classic failure is
  discovering three months later that the feed silently went delayed — voiding
  every L2/L3 conclusion built on it. A daily check makes that a one-day loss.
