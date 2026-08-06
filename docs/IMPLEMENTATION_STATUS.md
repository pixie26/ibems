# Implementation status — v0.1.5.dev0

## Reviewed IB-free core

Implemented and reviewed:

- event/state models, SQLite append-only journal and atomic decision/execution writes;
- target-position controller with exact order ownership;
- cancel-confirm-stable-reconcile-before-replace lifecycle;
- explicit `BrokerSnapshot.is_stable` gate: an unstable snapshot can never restore `SYNCED`;
- durable HALT causes and atomic exact-cause acknowledgement; acknowledgement does not resume the live process;
- EOD lifecycle, missed/deferred decisions, restart-safe risk counters and strict config validation;
- FakeBroker fault injection, async serialized bridge, watchdog PID identity guard;
- journal auditor and deterministic randomized soak.

Validation in the review environment:

```text
122 deterministic pytest cases: PASS
5 Hypothesis-gated tests: NOT RUN (Hypothesis unavailable)
deterministic soak 150 seeds x 100 actions: PASS
compileall: PASS
demo: PASS
editable install: PASS
```

A 300 x 150 soak was not completed within the execution limit and is not claimed as passed.

## Gate B1 blockers

- the Gate B1 Hypothesis campaign must actually run (`IB_GATE_B1_PROPERTY=1`);
- process-level crash windows need real subprocess `SIGKILL` tests;
- engine main/process lifecycle and journal-unavailable shutdown path remain incomplete;
- invariant 19/20/21 evidence and all 22 P/R/A rows must be reviewed complete;
- every generated journal must pass the offline auditor.

## Gate B2 blockers / unverified modules

- `IbAdapter.place_order` and `cancel_order`;
- real IB callback/error mapping;
- a measured IB stable-snapshot barrier across positions, open orders and executions;
- quote-recorder subscriptions and isolation;
- emergency-flatten broker calls;
- broker-time preflight and documented-vs-observed matrix.

## Safety label

```text
DO NOT CONNECT TO IB PAPER OR LIVE YET
Gate B1 not passed
```
