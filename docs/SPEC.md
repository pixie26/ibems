# SPEC — IB Execution Platform V1

**Status: FROZEN as of 2026-08-06.**

From this point, the only admissible form of objection is *"invariant N is
violated by X."* Objections of the form *"the architecture could be better"* are
out of scope. Changing anything in this document requires an ADR.

The reason for that rule is not bureaucratic. Execution platforms rarely die of
bad design; they die of design that never finished. Phase 0 is timeboxed to four
weeks (§8) for the same reason.

---

## 1. Scope

V1 is a **single-account, single-strategy, single-symbol** platform whose only
job is to turn a `TargetPosition` into broker state, safely, and to know at all
times whether it succeeded.

It is explicitly **not**: an EMS, a multi-strategy allocator, a smart order
router, or a capacity model.

Out of scope for V1, each with a reason:

| Excluded | Why |
|---|---|
| MOC / auction orders | Irrevocable past cutoff, conflicts with mutable targets; IB paper does not support them, so no meaningful validation is possible. ADR-003 |
| In-place order modification | Introduces a fill-vs-modify race. cancel-then-new is provable. ADR-002 |
| Multi-strategy attribution | Requires shadow positions; account-level position becomes ambiguous. ADR-001 |
| Automatic watchdog flatten | Split-brain risk exceeds the risk it removes at V1 size. ADR-004 |
| VWAP/adaptive/iceberg/bracket | Large state space, no current need |

### 1.1 The two gates are separate

```
Operational gate:  can the platform run safely?
Economic gate:     is the strategy worth money after real execution costs?
```

Passing the first never implies the second. This document covers only the first.
The economic gate is constrained by inference that the platform cannot improve:
a short post-publication window, an incomplete cost model, and P&L concentrated
in a handful of tail days.

---

## 2. State vector

Four **orthogonal** dimensions. Not one flat enum.

```
link_state       : CONNECTED | DEGRADED | DISCONNECTED
sync_state       : UNVERIFIED | SYNCING | SYNCED
operating_mode   : NORMAL | STOP_NEW | FLATTEN_ONLY | HALTED
order_state[s,y] : IDLE | INTENT_COMMITTED | SUBMISSION_UNCERTAIN
                 | PENDING_ACK | WORKING | PENDING_CANCEL
                 | TERMINAL_UNRECONCILED
```

A flat enum cannot express `WORKING + disconnected + closing-only` without
combinatorial explosion, and the combination it most often gets wrong is
*"allowed to close but not to open"* — which is exactly the state you are in
during an incident.

`sync_state` exists because **a socket being up is not the account being
trustworthy.** After IB error 1101 the link is `CONNECTED` and the subscriptions
are gone. Without a separate sync dimension, that state is indistinguishable
from a healthy one.

`RECONCILING` and `FLATTENING` are deliberately **not** states:

- reconciling is the mandatory side effect of a `link_state` transition
- flattening is `operating_mode = FLATTEN_ONLY` plus `desired_target = 0`

Flatten *cause* is still recorded, because it changes price aggressiveness and
post-failure handling: `EOD_FLATTEN | RISK_FLATTEN | MANUAL_FLATTEN |
RECOVERY_FLATTEN`.

### 2.1 Coupling rules

```
C1  order_state ∈ {SUBMISSION_UNCERTAIN, TERMINAL_UNRECONCILED}
        ⇒ sync_state := UNVERIFIED

C2  any broker write
        requires link_state == CONNECTED and sync_state == SYNCED

C3  opening (increasing risk)
        requires operating_mode == NORMAL

C4  closing (reducing risk)
        permitted in NORMAL, STOP_NEW, FLATTEN_ONLY
        forbidden in HALTED
```

C1 is the rule that keeps the orthogonality honest. Without it you get *"order
state says unknown, sync state says SYNCED, so the write goes through."*

---

## 3. Interfaces

A strategy may say exactly one thing:

```python
TargetPosition(
    strategy_id, symbol, target_quantity,
    decision_id,          # idempotency key
    valid_until,          # hard expiry, never extended
    metadata,
)
```

A strategy may **never** specify: order id, current position, whether to cancel,
whether this is a reversal, order type, or what to do after a partial fill.
Those are platform responsibilities. Nothing else changes if the signal engine
is replaced.

Platform → caller responses: `ACCEPTED | REJECTED_DUPLICATE | REJECTED_RISK |
REJECTED_STALE | REJECTED_STATE | PARTIALLY_FILLED | FILLED | CANCELLED | MISSED`.

---

## 4. Two independent triggers

Never conflated:

| Trigger | Frequency | Handling |
|---|---|---|
| Target changed | rare (~1 per 30 min) | cancel → await terminal → **fresh broker snapshot/reconcile** → recompute from new target |
| Reprice timeout | common | cancel → await terminal → **fresh broker snapshot/reconcile** → **same** target, next rung |

The reprice ladder is **bounded** (`max_attempts`, default 3). Unbounded
cancel-then-new is chasing, and this strategy's P&L lives in exactly the fast
markets where chasing costs most.

A clean cancel callback is not sufficient evidence to size the replacement: a
fill may have occurred in the cancel race window. Every confirmed cancellation
therefore forces reconciliation before another order. A rejected cancel is
recorded as non-terminal `CANCEL_REJECTED`; if the broker still shows the order
working after reconciliation, the engine HALTs rather than retrying in a loop.

Ladder exhaustion by purpose:

| Purpose | On exhaustion |
|---|---|
| Opening | abandon; record `DECISION_MISSED(REPRICE_EXHAUSTED)` |
| Adjusting | accept partial, wait for next target |
| EOD flatten | continue with progressively more aggressive pricing |
| Risk flatten | prioritise risk reduction, widest permitted protection |

---

## 5. At-most-once submission

There is no atomic transaction spanning our database and the broker. We choose:

> **When we cannot know whether an order was delivered, we would rather miss a
> trade than send a second one.**

Write order, which never varies:

```
1. atomically consume decision_id + append TARGET_RECEIVED
2. commit ORDER_INTENT_COMMITTED
3. commit SEND_ATTEMPT_STARTED  ← durable BEFORE the call
4. in-memory order_state := PENDING_ACK
5. broker.place_order(...)
6. commit SEND_CALL_RETURNED / SEND_CALL_FAILED
7. broker callbacks fill in permId, working state, executions
```

During the live call the state is `PENDING_ACK`; setting it to
`SUBMISSION_UNCERTAIN` before the write would itself force sync to UNVERIFIED and
make the write illegal. On restart, a durable `SEND_ATTEMPT_STARTED` without a
provable clean terminal result folds to `SUBMISSION_UNCERTAIN`: reconcile, never
resend. If the target has expired, abandon. If it still cannot be proven
either way, a human decides.

The reverse order — call first, journal after — produces an order the broker
knows about and we do not. That is the failure this ordering exists to prevent.

---

## 6. Reconciliation

> **The broker is the authority on facts. The journal is the authority on
> meaning.**

Boot sequence:

```
1. replay journal → expected positions, open intents, residuals
2. connect
3. sync_state := SYNCING
4. fetch positions, open orders, executions
5. adopt broker-proven facts the journal is missing
6. recompute expected
7. expected == actual  → SYNCED
8. otherwise           → HALTED
```

Step 5 matters: a crash between the broker filling and us writing it down is
**normal**, not an anomaly. Without it, every crash becomes a false HALT.

The comparison baseline is **journal-expected, not zero**. A residual from a
failed EOD flatten is a *known risk event*, not an unknown position — it comes
up `SYNCED` and `FLATTEN_ONLY`. The system may know what it holds; it may not
pretend yesterday's incident did not happen.

External broker facts are attributed only by an exact durable identity match:
`orderRef`, and `permId` as soon as it is known. A whitelisted strategy prefix is
not ownership. Anything not explainable by a committed intent → immediate HALT.
Never adopted, never ignored. Tolerance is **0 shares**.

### 6.1 Order identity chain

```
decision_id  strategy-level idempotency key
intent_id    our WAL primary key
orderRef     compact strategy tag + fixed hash; exact mapping lives in journal
orderId      per-session API handle
permId       broker-side identity, stable across sessions ← persist this
execId       one per partial fill; corrections increment the trailing segment
```

`orderRef` is user-defined and IB does not enforce uniqueness on it, so it is a
transport attribution handle, never a primary key. The implementation keeps it
compact rather than depending on unverified truncation behaviour.

---


### 6.1 Stable reconciliation barrier

A reconciliation snapshot is not trustworthy merely because positions, open orders
and executions were each requested once. Delayed callbacks can make that composite
view internally inconsistent. `BrokerSnapshot.is_stable` is therefore a mandatory
control field. It may be `True` only after the adapter has completed and measured a
broker-side barrier covering positions, open orders and executions.

```text
snapshot.is_stable == False → diagnostic use only → sync_state remains UNVERIFIED
snapshot.is_stable == True  → reconciliation may evaluate whether SYNCED is justified
```

Gate B2 must document the exact IB completion/watermark protocol and demonstrate it
under delayed callback and reconnect tests. A single naïve snapshot call must never
restore `SYNCED`. See ADR-008.

## 7. Invariants (22)

Gate B1 requires each to exist **three times** (invariant 20):
**(a)** property/adversarial test · **(b)** runtime assertion or structural
enforcement, fail-closed · **(c)** offline journal auditor.

This is an acceptance criterion, not a current completion claim. See
`docs/INVARIANT_COVERAGE.md`.

(c) is the one that gets skipped and the one that matters most: a green test
suite proves the code was right on inputs someone imagined; only the auditor
proves the system that *actually ran*, on the day it ran, obeyed the spec. It is
a required end-of-day artifact from Phase 1 onward.

| # | Invariant |
|---|---|
| 1 | Each `decision_id` accepted at most once |
| 2 | Every broker write preceded by a durable intent |
| 3 | At most one unterminated intent per `(strategy, symbol)` |
| 4 | No second order while `PENDING_ACK` |
| 5 | No replacement order while `PENDING_CANCEL` |
| 6 | No normal broker write unless `sync_state == SYNCED` |
| 7 | Opening requires `operating_mode == NORMAL` |
| 8 | `FLATTEN_ONLY` permits only `target = 0` |
| 9 | Working orders resolved before flattening |
| 10 | After restart, reconcile before any send |
| 11 | Expired targets are never sent, including after reconnect |
| 12 | Each raw `execId` booked once; corrections are new events |
| 13 | A missing fee is a legal intermediate state |
| 14 | Unexplained position / order / execution → HALT |
| 15 | An **explained** residual → `FLATTEN_ONLY`, not "unknown" |
| 16 | Hard caps on orders/minute, orders/day, cumulative shares and notional |
| 17 | Every intent records its `risk_config_hash` |
| 18 | Callback exceptions fail closed |
| 19 | `max_position` sized so an unflattened position survives overnight |
| 20 | Every invariant implemented three ways (a/b/c) |
| 21 | Risk self-test at startup: must-reject orders are rejected, or refuse to start |
| 22 | A restart must not clear a HALT. Only an attributed acknowledgement does |

**Invariant 22 exists because documentation is not a control.** An acknowledgement
must atomically reference the exact latest durable HALT cause. A stale acknowledgement
must fail, a later cause while already HALTED must advance the acknowledgement token,
and acknowledgement must not resume the current process; restart and reconciliation
remain explicit human actions. The RUNBOOK said "HALT is correct behaviour, do not
restart to clear it" and ADR-004 made
restart manual for the same reason, but nothing enforced either, and every
version through v0.1.3 cleared HALT on restart. The dangerous path is the
likely one: watchdog kills the engine, someone restarts it, trading resumes
with the root cause undiagnosed. Clearing now requires a named operator and a
written resolution, both journalled.

**Invariant 19 is load-bearing.** The watchdog does not auto-flatten (ADR-004),
and that is only safe because an unattended position is survivable overnight.
The two are coupled: **any change to position size must revisit the watchdog
design in the same commit.**

System-level principle:

> **Safety over liveness. When we do not know, we stop.**

This is unconditionally correct at 1–5 shares because the cost of not acting is
approximately zero. That premise is size-dependent. At larger size the answer is
not to reverse the principle but to guarantee that human escalation *is triggered
within a bounded time* — hence the escalation deadline in §9.

---

## 8. Gates

No dates. Gates, with one exception.

**Gate B0 — design freeze.** This document is frozen. Implementation conformity
remains open and may block the next gate.

**Gate B1 — FakeBroker. CURRENT STATUS: NOT PASSED.** Deterministic state
machine, no IB. Must cover:
delayed ack · duplicate callback · callback reorder · partial fill ·
fill-before-cancel · cancel reject · cancel timeout · disconnect · reconnect ·
process kill · stale snapshot · execution correction · late fee · external order.

Acceptance: 0 duplicate orders under any interleaving · 0 silent state loss ·
every unknown state fails closed · event replay reproduces state exactly ·
auditor clean on every generated run · all 22 rows in the invariant coverage
matrix complete.

Before B2, all callbacks and strategy commands must pass through
`AsyncControllerBridge`. The synchronous Controller may never execute on the IB
event-loop thread because durable journal commits wait for fsync.

**Gate B2 — IB paper, manual targets.** SPY, 1–5 shares, marketable limit,
no MOC, no automatic signal.

Deliverable is *not* "it works". It is a table:

| documented behaviour | observed | matches? |
|---|---|---|
| orderRef survives into executions | ? | ? |
| permId stable across sessions | ? | ? |
| 1101 vs 1102 semantics | ? | ? |
| execId correction format | ? | ? |
| paper fills at top of book | ? | ? |

Every behavioural claim taken from IB documentation is an **assumption** until
this table exists. IB docs lag the implementation.

**Gate B3 — automated failure drills.** `kill -9` after WAL / after placeOrder /
after partial fill · Gateway restart · 1101 resubscription · cancel timeout ·
external order · EOD residual · callback exception · journal I/O failure ·
token bucket under a runaway loop.

**Gate C — shadow signal.** No orders. Three layers:

| Layer | Question | Acceptance |
|---|---|---|
| L1 | Is the system deterministic? | live vs replay 100% identical |
| L2 | Is live input semantically the same as research input? | every material difference classified; UNKNOWN class has no P&L impact |
| L3 | Does the edge survive a data source change? | no parameter retuning; report full/pre/post, disagreement days, tail-day P&L delta |

L1 alone proves nothing about correctness — replaying your own capture always
agrees with itself. **L2 is the real test**, and its acceptance is not "zero
difference" (unrealistic) but "every difference classified and quantified":
`TIMESTAMP_CONVENTION · BAR_BOUNDARY · ODD_LOT · TRADE_CONDITION ·
FILTERED_TRADE · MISSING_TICK · DISCONNECT_GAP · SESSION_CALENDAR ·
PRICE_ADJUSTMENT · UNKNOWN`.

L3 splits:

- **L3a** — IB historical minute bars, immediate. *Can only falsify, never
  exonerate*: IB historical bars are filtered, live tick-by-tick is not, so L3a
  and L3b test different hypotheses. L3a failing is a stop signal; L3a passing
  guarantees nothing about live.
- **L3b** — data captured live by the recorder.

Coverage: ≥20 full sessions, one half-day, one synthetic disconnect gap, one
high-volatility day.

**Gate D — minimum live.** Entry is conditions, not a date: B1 + B2 + B3 passed,
L1 passed, L2/L3 found nothing fatal, emergency flatten drilled.

1 share. No leverage. Position capped by overnight survivability. No MOC.
Dedicated account. Hard order-count and cumulative-fill caps.

**This is a data-collection experiment, not a capital deployment.**

**Gate E — cost model and funding decision.** See §10.

**The one date: Phase 0 is timeboxed to four weeks.** Not past Gate B1 by then →
cut scope, starting with the reprice ladder (degrade to single attempt, then
abandon).

---

## 9. Time and escalation

All `valid_until` checks and EOD deadlines ride on the local clock. Startup
verifies local clock against IB server time and **refuses to start** past
threshold; periodic checks trip `STOP_NEW`. A silently drifting clock produces
stale-target rejects and missed flattens, and neither announces itself.

Half days move the close to 13:00 ET. A hardcoded 15:50 flatten never fires and
nothing errors — you simply wake up long. The holiday table is reviewed annually
(RUNBOOK §4).

**Escalation deadline (default close − 15 min):** past this point, an unresolved
`sync_state != SYNCED` or `order_state ∈ untrustworthy` is a **human page**, not
a log line. A human verifies broker state in TWS and decides on manual flatten.

---

## 10. Cost model — three ledgers

```
1. Deterministic fees      commission, SEC Section 31, TAF, borrow, financing
                           → published rate tables, point-in-time. NOT measured
                             from 5-share trades.
2. Observable exec costs   half-spread, arrival shortfall, latency, reject rate,
                           MISS RATE
                           → measurable at 1–5 shares
3. Modeled size costs      market impact, queue depletion, auction capacity
                           → NEVER extrapolated from 5 shares
```

### 10.1 The availability term

Transient inability to act is first recorded as
`TARGET_DEFERRED(DISCONNECTED/NOT_SYNCED/ORDER_STATE_BLOCKED/...)`. It becomes a
terminal `DECISION_MISSED` only when the target expires or is definitively
rejected (`EXPIRED · RISK_BLOCKED · BROKER_REJECTED · MODE_BLOCKED ·
REPRICE_EXHAUSTED`). Daily analysis joins the last defer reason to the final
outcome. A target that executes within `valid_until` is not a miss.

A backtest trades every decision. Production does not. For straddle-shaped P&L,
**misses on tail days can dominate every slippage assumption in the model** — and
tail days are exactly when infrastructure is least healthy. This term is
currently absent from the research cost model entirely.

### 10.2 Asymmetric update rule — pre-registered

Tail-day cost stress is a *scenario*, not an upper bound:

```
tail_cost_stress = tail quoted half-spread
                 + latency adverse move   (proxy: price change at +1s/+3s/+5s)
                 + modeled impact stress
                 + deterministic fees
```

with a floor of `K × normal-state variable cost`, `K ∈ [3,5]` pre-registered.

```
observed > stress   → raise the assumption immediately, re-evaluate
observed < stress   → keep the assumption
lower it ONLY IF    tail effective sample size ≥ pre-registered threshold
                    AND CI width ≤ pre-registered threshold
```

Rationale: tail days occur perhaps 5–10 times a year, so a year of Gate D
produces a single-digit tail sample. Without asymmetry, a lucky quiet year reads
as "execution is cheap" → size up → the first real tail day gives it all back.

Note the structural echo: this is the same problem as the strategy's own
inference — a short window, heavy tails, and too few effective observations. The
cost estimate inherits the disease it is meant to cure. Asymmetry is the
mitigation.

---

## 11. Processes

```
quote_recorder      read-only, always on, isolated, own rate limiter
execution_engine    the ONLY normal broker writer
watchdog            alert + SIGTERM/SIGKILL. No restart, no orders, no mode writes
emergency_flatten   human-triggered, dedicated clientId, monthly drill
```

Client IDs: 31 execution · 32 emergency · 33 recorder. Never 0 for trading —
clientId 0 binds manually-placed TWS orders, and binding can trigger
cancel/resubmit, costing queue position on an order we did not create.

---

## 12. Language

**Python 3.12 + ib_async + SQLite WAL + pytest + Hypothesis.**

The argument is a **safety** argument, not a performance one: a single-threaded
event loop eliminates an entire class of concurrent state bugs, and this
system's whole risk surface is state. Latency is irrelevant here — authority
lives inside IB Gateway (a Java process) and no language choice reaches past it.

The cost is real and accepted knowingly: anything blocking sits on the critical
path. Hence the dedicated journal writer thread, measured fsync latency
published in status, and `STOP_NEW` on breach. **Any change that puts a blocking
call back on the event loop revokes this argument.**

`ib_async` is the maintained fork; `ib_insync` was archived in 2024.
