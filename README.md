# ib-execution-platform v0.1.5.dev0 — Phase 0 reviewed

> **`DO NOT CONNECT THE TRADING ADAPTER TO IB PAPER OR LIVE YET.`**
> The trading IB adapter and emergency-flatten broker calls remain unimplemented.
> The read-only recorder is implemented but has not connected to a Gateway. This
> package is not yet a trading system.

**v0.1.5 accepts v0.1.4's central fix — HALT must survive restart — and closes
two additional safety gaps found during review:** HALT acknowledgement is now an
atomic acknowledgement of the exact latest durable cause, and only a broker
snapshot that has completed an explicit stability barrier may restore `SYNCED`.
See [`docs/FINAL_REVIEW_V014_ZH.md`](docs/FINAL_REVIEW_V014_ZH.md) and
[`docs/CHANGES_v0.1.5.md`](docs/CHANGES_v0.1.5.md).

A thin, single-account IBKR execution platform. Strategies emit a target position:

```text
strategy → TargetPosition → risk → OMS → broker
```

The governing principle remains: **safety over liveness — when state is not
provably trustworthy, stop.**

Full specification: [`docs/SPEC.md`](docs/SPEC.md).  
Final execution plan: [`docs/FINAL_EXECUTION_PLAN_ZH.md`](docs/FINAL_EXECUTION_PLAN_ZH.md).  
Decisions: `docs/ADR-001` through `ADR-009`.  
Operations: [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

## Status — read this first

| Component | Status |
|---|---|
| IB-free core | **Reviewed prototype.** 138 non-property tests pass on Python 3.12.13, including seven subprocess force-kill windows and six journal/queue failures. Gate B1 remains **not passed**; see the explicit blockers in `docs/INVARIANT_COVERAGE.md`. |
| Hypothesis module | 5 tests pass under both the default profile and the formal `gate` profile. The two generated tests each passed 1,500 examples with seed `2026080601`; manifest: `artifacts/gate_b1/20260806T142435Z/manifest.json`. |
| Read-only recorder | Subscription, raw storage, Parquet, health and hash pipeline implemented and locally tested; **never connected to a Gateway**. |
| Trading `ib_adapter`, emergency-flatten broker calls | **UNVERIFIED skeletons.** Never connected to a Gateway. |

**Safety label: do not connect the trading adapter. The isolated Read-Only Recorder may connect after its Gateway/entitlement preflight.**

```bash
pip install -e ".[dev]"
pytest -q
pytest -q -m property --hypothesis-profile=gate
python scripts/run_gate_b1.py
python -m ib_execution.quote_recorder --root data/recordings --port 4002
python scripts/demo.py
python scripts/deterministic_soak.py --seeds 150 --actions 100
```

The deterministic soak passed at 150 seeds × 100 actions. A larger 300 × 150 run
was not completed within the review execution limit and is not claimed as passed.

---

## Why the IB-free core is the whole point

Execution systems fail on **state recovery**, not logic. The happy path is easy.
What breaks desks is: a crash after submission but before the callback; a fill
landing between your cancel and its acknowledgement; callbacks arriving out of
order; a Gateway restart mid-position.

IB paper reproduces almost none of that. It will not reorder callbacks on
demand, will not fill an order inside your cancel window, and will not crash at
a chosen instruction.

So the state machine is synchronous and clock-injected, and `FakeBroker` can
inject twelve fault classes deterministically. Every interleaving is
reproducible from a test.

**Multiple real lifecycle bugs were found this way**, none of which IB paper reliably surfaces:

1. **Fill racing a cancel.** A fill completing during `PENDING_CANCEL` returned
   the leg to `IDLE`, which immediately recomputed delta and issued a blind
   `SELL 6`. Now: `TERMINAL_UNRECONCILED`, re-read the broker, then decide.
2. **Restart duplicating a live order.** Reconciliation only updated legs that
   already existed in memory. After a crash the leg table is empty, so the first
   target saw `position=0, working=0` and re-sent an order the broker already
   had.
3. **Reversal blocked by config.** A flip needs `2N` shares in one order. With
   `max_order_shares == max_position_shares` every reversal was silently
   RISK_BLOCKED — no error, just a strategy that quietly never flips. Found by
   the demo script, now rejected at config validation.
4. **EOD flatten cancelled but never converged to zero.** A working order was
   cancelled before target zero was installed, so the old target was re-evaluated
   and no closing order was sent.
5. **Cancel reject falsely terminalized ownership.** The still-working broker
   order was then classified as external during reconciliation.
6. **A late execution updated the journal but not memory.** The next order could
   therefore be sized from a stale position.
7. **HALT acknowledgement could clear the wrong incident.** A stale operator
   screen could acknowledge an earlier HALT after a newer cause had arrived. The
   journal now uses an atomic compare-and-set against the exact active HALT token;
   acknowledgement never resumes the live process.
8. **A non-atomic broker snapshot could authorize a duplicate replacement.** A
   cancelled order appeared absent, a replacement was sent, then a delayed fill
   from the old order arrived and breached the position limit. `SYNCED` now requires
   an adapter-proven stable snapshot barrier; diagnostic snapshots cannot trade.

---

## Core ideas

**Target positions, not orders.** A strategy never says "buy 200". It says
"I want to be at -200". The platform derives the rest, so replacing the signal
engine changes nothing here.

```
delta = target − broker_position − signed_working_remaining
```

This is an **invariant describing a settled system**, not a control rule to be
evaluated on a timer. Doing the latter double-sends during the window where an
order exists at the broker but not yet in `openOrders()`.

**Four orthogonal state dimensions**, not one flat enum:

```
link_state  ×  sync_state  ×  operating_mode  ×  order_state[strategy, symbol]
```

`sync_state` exists because a socket being up is not the account being
trustworthy — after IB 1101 the link is `CONNECTED` and subscriptions are gone.
Orthogonality is what lets `FLATTEN_ONLY` permit closing while forbidding
opening; a flat enum gets exactly that case wrong.

**Broker is the authority on facts; the journal is the authority on meaning.**
Reconciliation compares broker state against *journal-expected*, not zero. A
residual from a failed EOD flatten is a known risk event, not an unknown
position: the system boots `SYNCED` + `FLATTEN_ONLY`. It may know what it holds;
it may not pretend yesterday's incident did not happen.

**At-most-once.** No atomic transaction spans our database and the broker, so we
choose which error to prefer. A duplicate creates a position nobody sized. A
miss costs one opportunity. Journal the intent *before* the call; on uncertainty,
reconcile — never retry. Misses are tracked as a first-class cost-model term.

**Gate B1 requires every invariant to exist three times:** property test,
runtime assertion/structural enforcement, and an offline journal auditor. That
requirement is not yet complete; the current coverage matrix is explicit rather
than implied. See [`docs/INVARIANT_COVERAGE.md`](docs/INVARIANT_COVERAGE.md).

```bash
python -m ib_execution.auditor data/journal.db   # required end-of-day artifact
```

---

## What the platform will not do

| | Why |
|---|---|
| Watchdog auto-flatten | Split-brain risk exceeds the risk removed. Alert and `SIGKILL` only; restart is human. ADR-004 |
| MOC / auction orders | Irrevocable past cutoff, and IB paper cannot validate them. ADR-003 |
| In-place order modification | Fill-vs-modify race. `cancel → confirm → reconcile → new` is provable. ADR-002 |
| Share an account | Account-level positions become ambiguous. Anything unattributable → HALT, 0-share tolerance. ADR-001 |

The watchdog restraint is only safe because of **invariant 19**: position limits
are sized so an unflattened position survives overnight. The two are coupled —
changing size means revisiting ADR-004 in the same commit.

---

## Credentials

Never in this repo, never in `config/`, never in a chat window. IB Gateway
authenticates at the Gateway process; this platform never sees a username or
password. `preflight` scans for leaked secrets before every session.

**If a password has ever been pasted anywhere shared, rotate it.** Paper
credentials get reused on live accounts more often than anyone admits, and that
is the actual attack path.

---

## Next steps, in priority order

**1. Economic Gate A (Week 0, highest information value).** Section 31 as a
point-in-time series, the missing EOD fill price, and block bootstrap on the
post-publication window. A plausible outcome is *"the evidence does not support
deployment"* — in which case the SPY integration path stops before IB work.

**2. Recorder (parallel, calendar-constrained).** Full-session read-only capture,
isolated from the trading path. Tail-day observations cannot be recovered later,
so recorder implementation starts in parallel rather than waiting for Gate A.

**3. L3a early.** IB historical minute bars, no retuning. It can only falsify,
never exonerate: IB historical bars are filtered and live tick data is not, so
L3a and L3b test different hypotheses. First verify IB's historical depth covers
the pre-period and that pacing is workable — if not, that is a data-vendor
purchase decision with lead time, and it should be made now rather than at
Gate C.

**4. Then Gates B1 → B2 → B3 → C → D.** Phase 0 timeboxed to four weeks; past
that, cut scope starting with the reprice ladder.

The platform's current second consumer is explicitly **NONE_CONFIRMED**. QQQ is
not independent justification because it is the same broad intraday-momentum
thesis. If Gate A returns NO_GO/INSUFFICIENT_EVIDENCE and no independent second
consumer is named, stop before implementing the IB adapter; retain only the
recorder and reusable FakeBroker core.
