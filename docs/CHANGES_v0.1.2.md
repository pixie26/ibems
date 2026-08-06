# v0.1.2 — verification of the review, plus four fixes

## Verdict on the v0.1.1 review

**Seven of eight Critical findings are real, and two of them are serious.**
The eighth has a wrong diagnosis but the correct prescription.

| # | Claim | Verified |
|---|---|---|
| 1 | Normal send path cannot work | **Diagnosis wrong** — the path works; see below |
| 2 | Restart may duplicate orders | **REAL, serious** — reproduced |
| 3 | Cancel attempted while disconnected | Real |
| 4 | 1101 recovery too weak | Real |
| 5 | Ownership forgeable by prefix | Real |
| 6 | External execution silently booked | Real |
| 7 | Two crash windows in claim-then-commit | Real |
| 8 | Reject loop bypasses runaway caps | **REAL, serious** |

### On Critical 1

The claim that the normal path *must* fail is not correct. The write gate is
evaluated in `_evaluate` before `_send`; there is no second assertion at the
moment of the broker call. v0.1.0 places orders successfully in 64 tests.

But the underlying critique holds and the fix is right. `SUBMISSION_UNCERTAIN`
was overloaded for two different things — *"we are calling the broker now"*
(routine) and *"the call failed with unknown delivery"* (an incident). The
normal path therefore passed transiently through a state the spec calls
untrustworthy, and the auditor's invariant-6 check only missed it because
`SEND_ATTEMPT_STARTED` happens to be journalled before the `SYNC_STATE_CHANGED`.
That is ordering luck, not correctness. Splitting `PENDING_ACK` from
`SUBMISSION_UNCERTAIN` is the right repair.

### On Critical 2 — reproduced

```
v0.1.0:   target -2 after restart -> 2 LIVE orders, 0 cancels
v0.1.1:   target -2 after restart -> 1 order, 1 cancel
```

Reconciliation restored `working_signed` but not `live_intent` or `order_state`,
so `order_state` stayed `IDLE` while an order was live at the broker. `IDLE` is
not in `BLOCKS_NEW_ORDER`, so invariant 3's enforcement was silently disarmed.

**Why a 64-test suite missed it:** the restart regression test re-sent the *same*
quantity, so delta computed to zero and no second order was attempted. The bug
only appears when the post-restart target differs — the normal production case.

Under a randomized soak (200 trials × 60 actions, restarts included):

```
v0.1.0:  84 audit findings, 1109 moments with 2+ live orders
v0.1.1:   0 audit findings,    0
```

**The auditor was never wrong. The generated inputs were too narrow.** That is
the most useful thing this review produced, and it is a lesson about test
inputs, not about detectors.

---

## Fixes added in v0.1.2

### 1. EOD residual is now recorded at the time (gap in both versions)

Reproduced against v0.1.1: Gateway dies before the close, position 3 held,
`EOD_FLATTEN_STARTED` written, **no `EOD_FLATTEN_FAILED` ever written**.

Consequence: the position is carried overnight with no durable explanation, and
tomorrow's reconciliation sees a position the journal cannot account for and
false-HALTs on a residual we actually understood. Invariant 15 only means
something if the explanation exists.

`_record_eod_residual` now runs **before** the connectivity and mode guards, so
it fires when disconnected, when `HALTED`, and after the close — precisely the
situations the earlier early-returns skipped. Once per session per leg.

> A silent overnight position is as dangerous as an unknown one, and unlike an
> unknown one it is entirely preventable.

### 2. Auditor coverage 14 → 18 of 21

Added invariants **9** (flatten after working orders resolved), **13** (missing
fee is benign), **15** (recorded residual boots FLATTEN_ONLY), **16** (runaway
caps).

**Invariant 16 mattered most.** The runaway breaker is the single most important
control in the system — the disaster case is not one bad order, it is a loop
emitting ten thousand — and it had no offline proof at all.

19, 20 and 21 remain unaudited **and are declared so**: overnight sizing,
three-way implementation and the startup self-test are structural properties
that cannot be proven from an event log. They are enforced at config validation
and startup instead.

### 3. `restart` added to the generated action set

This omission is the entire reason the duplicate-order bug survived. Verified
load-bearing: running the updated property test against v0.1.0 fails in seconds
with `INVARIANT 3: second live intent ... while ... still open`.

Any process-lifecycle event that can occur in production belongs in that list.

### 4. Watchdog PID-reuse guard

PIDs are recycled. On a busy host the engine dies, its PID is reused by
something unrelated, and a watchdog trusting the number alone SIGKILLs an
innocent process while the real engine is already gone. Status now publishes
process start time; `kill_engine` refuses to signal on mismatch.

---

## Two harness defects found and fixed

Both were in the test equipment, not the system. They matter anyway: a fake
broker that lies sends people chasing bugs that do not exist.

- **Auditor false positive.** The terminal-event set was copy-pasted into four
  places in `auditor.py`; the fifth copy (my invariant-9 check) omitted
  `ORDER_ABSENT_CONFIRMED` and flagged correct behaviour. Now a single
  `TERMINAL_ORDER_EVENTS` constant in `models.py`, referenced everywhere, so the
  auditor and controller cannot drift.
- **FakeBroker resurrected cancelled orders.** A fill delivered after a cancel
  reset status from `Cancelled` back to `Submitted`, producing phantom
  simultaneous live orders in the harness. Terminal statuses are now terminal.

---

## Verification

```
full suite            93 tests pass (5 Hypothesis property tests DO run here)
randomized soak       4 seeds x 200 trials x 60 actions, restarts included
                      1358 orders, 1197 executions
                      0 audit findings
                      0 moments with 2+ live orders
                      0 exceptions
demo + preflight      pass
```

---

## Unchanged, and still the point

**`DO NOT CONNECT TO IB PAPER OR LIVE YET.`** `IbAdapter.place_order`,
`cancel_order`, callbacks, error-code mapping, the recorder subscription and the
emergency-flatten broker calls are all still unimplemented.

And the priority is unchanged: **Gate A does not need any of this.** Section 31
as a point-in-time series, the missing EOD fill price, and block bootstrap on
the post-publication window are hours of work and can plausibly return
`INSUFFICIENT_EVIDENCE` — which for capital deployment is a no-go, and which
would save a year.

Two packages of review have now improved the platform's safety. Neither has
moved that number.
