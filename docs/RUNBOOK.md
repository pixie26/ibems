# RUNBOOK

Operational procedures. Terse on purpose — this is read during an incident.

---

## 0. Credentials

**Never in this repository. Never in `config/`. Never in a chat window.**

IB Gateway authenticates at the Gateway process, not through the API. This
platform never handles a username or password.

```bash
# outside the repo, mode 600, or a secrets manager
export IB_ACCOUNT=DU1234567   # provenance-allow: documentation placeholder, not a real account
```

**If a password has ever been pasted into a chat, a ticket, a screenshot or a
commit — rotate it now.** Assume it is public. This includes paper accounts:
paper credentials get reused on live accounts more often than anyone admits, and
that is the actual attack path.

---

## 1. Daily — engine

Before Gate B2 this command is intentionally incomplete and will fail the
broker-clock check. Once the adapter is wired, run:

```bash
python -m ib_execution.preflight \
  --risk-config config/risk.yml --journal data/preflight.db
```

It must load the actual deployed risk file and refuses to start on: missing
broker time · clock skew > 2s · risk self-test failure · unreviewed holiday
table · fsync p99 above threshold. A default in-code RiskConfig is not a
production preflight.

After the close (**required artifact from Gate B2 onward**):

```bash
python -m ib_execution.auditor data/journal.db
```

During Phase 0 the auditor reports **partial coverage**. Gate B1 is not complete
until `docs/INVARIANT_COVERAGE.md` has all 22 rows covered. Within the invariants
that are audited, any finding is an incident. Also review `decision_misses` —
that is the cost model's availability term.

---

## 2. Daily — recorder

**Current status: storage/health logic only; IB subscriptions are not yet
implemented. Do not assume data collection has started.** Once Gate R1 ships,
an automated report is pushed daily. Check:

| Field | Threshold |
|---|---|
| `market_data_type` | must be `LIVE` |
| `coverage_fraction` | ≥ 0.99 of RTH |
| `max_gap_seconds` | ≤ 30 |
| `clock_skew_seconds` | ≤ 2 |
| `disconnects` | reviewed, not just counted |

**Do not skip this because the recorder "just works".** The classic failure is
discovering three months later that the feed silently switched to delayed data —
and that every L2/L3 conclusion built on it is void. A daily check converts that
into a one-day loss.

---

## 3. Incident response

### 3.1 Engine HALTED

**A restart will NOT clear it (invariant 22).** The engine comes back up
HALTED and refuses to trade until someone acknowledges it by name:

```bash
python -m ib_execution.ack_halt --journal data/journal.db --show
# read the leading events and verify broker state in TWS, THEN:
python -m ib_execution.ack_halt --journal data/journal.db \
    --operator <your name> --resolution "<what you found and what you did>"
```

If you cannot explain the cause, do not clear it. Never script this.
If you want to script it, the thing to fix is whatever keeps producing HALTs.


HALT is correct behaviour, not a bug. Do not restart to "clear" it.

```
1. read the last INVARIANT_VIOLATION / RECONCILIATION_FAILED / CALLBACK_FAILURE
2. open TWS, read actual position and open orders with your own eyes
3. compare against the journal's expected position
4. decide: manual flatten, or leave the position and investigate
5. restart ONLY after the cause is understood
```

### 3.2 Watchdog killed the engine

The watchdog does not restart it (ADR-004). Deliberate.

```
1. why did the heartbeat stop? (fsync stall / crash / partition / OOM)
2. verify broker position in TWS
3. if flat: investigate at leisure
4. if not flat: decide on emergency_flatten
5. restart manually only after diagnosis
```

### 3.3 Approaching the escalation deadline with unresolved state

Past close − 15 min with `sync_state != SYNCED` or an untrustworthy
`order_state`, you get paged.

```
1. TWS. Read the real position.
2. Decide NOW whether to flatten manually. Do not wait for the engine to sort
   itself out.
3. Record the decision and the outcome in the journal.
```

Invariant 19 means an overnight position is survivable. It is not free.

### 3.4 Emergency flatten

**Current status: plan/confirmation scaffolding only; broker calls are not
implemented. Until Gate B2, flatten manually in TWS and record the incident.**

After the tool is implemented and paper-drilled, the intended command is:

```bash
python -m ib_execution.emergency_flatten \
    --account "$IB_ACCOUNT" --symbol SPY --journal data/journal.db --port 4002
```

Type the confirmation token exactly. `FLATTEN LIVE` for a live account. The
order of operations never varies; re-pull positions after cancels confirm before
calculating the closing quantity.

### 3.5 EOD flatten failed

Journal will contain `EOD_FLATTEN_FAILED(symbol, residual_quantity, reason)`.
This is a **legal, known** position, not an unknown one.

Next morning the engine boots `SYNCED` + `FLATTEN_ONLY`. It knows what it holds
and will not pretend yesterday did not happen. Clear manually after review.

---

## 4. Monthly

- [ ] **Emergency flatten drill on paper.** Non-negotiable. A recovery tool that
      has never been run is a file, not a tool.
- [ ] Review `DECISION_MISSED` counts by reason — the availability term
- [ ] Review fsync latency distribution
- [ ] Confirm the recorder's daily reports were actually read

## 5. Annually (each December)

- [ ] **Update the holiday and half-day tables in `calendar.py` for next year.**
      A stale table means a hardcoded flatten time that never fires on a half
      day. Nothing errors; you simply wake up long.
- [ ] Re-review invariant 19 against current position limits
- [ ] Re-review ADR-002 and ADR-006 if size has changed

---

## 6. Things that are correct and look alarming

| Symptom | Verdict |
|---|---|
| Engine refuses to trade after reconnect | Correct. 1101 means subscriptions lost; reconcile first. |
| Engine comes up HALTED after a restart | Correct. Invariant 22. Diagnose, then `ack_halt`. |
| A target was skipped as `EXPIRED` | Correct. A 10:00 signal is not a 10:20 trade. |
| `SUBMISSION_UNCERTAIN` and no retry | Correct. ADR-006. Reconcile, never resend. |
| Fill recorded, fee missing | Correct. Invariant 13. Fees arrive separately and late. |
| HALT on an externally placed order | Correct. ADR-001. Never adopt, never ignore. |
| Reprice ladder gave up after 3 attempts | Correct. Unbounded repricing is chasing. |
| Refused to reprice on a stale quote | Correct. A loop on a frozen quote walks you into the book. |

## 7. Things that are genuinely wrong

| Symptom | Action |
|---|---|
| Two orders live for one symbol | STOP. Invariant 3. Auditor should have caught it. |
| An `execId` booked twice | STOP. Invariant 12 is DB-enforced; this means corruption. |
| A send with no prior `ORDER_INTENT_COMMITTED` | STOP. Invariant 2. Write ordering is broken. |
| Auditor `FAIL` on any session | Incident. Do not trade the next session until resolved. |
| Position mismatch the journal cannot explain | Already HALTED. Investigate before restart. |


## Exact HALT acknowledgement (v0.1.5)

1. Read the current active HALT sequence from the journal/CLI.
2. Investigate and record operator plus resolution.
3. Acknowledge that exact sequence. A stale sequence must fail.
4. The running controller remains HALTED after acknowledgement.
5. Stop it manually, verify broker truth, restart, then complete reconciliation.
6. Only a stable snapshot may restore `SYNCED`. `snapshot_not_stable` is an incident, not permission to retry blindly.
