# v0.1.4 — one finding, and it was in every version so far

## Verdict on v0.1.3

**All eight claimed fixes verified working.** Each was reproduced against a
controlled scenario rather than accepted from the changelog:

| Fix | Verified |
|---|---|
| 1 — EOD flatten with a working order converges to zero | PASS (final position 0, FLATTEN_ONLY) |
| 2 — clean cancel must reconcile before replacement | PASS |
| 3 — CANCEL_REJECTED non-terminal, HALT if still working | PASS |
| 4 — target change does not inherit the reprice rung | PASS (attempt 2 → 0) |
| 5 — late execution updates position and forces resync | PASS |
| 6 — one working callback does not restore account SYNCED | PASS |
| 7 — TARGET_DEFERRED vs DECISION_MISSED | PASS, both directions |
| 8 — EOD lifecycle, watchdog PID, risk config, auditor | PASS |

Fix 7 deserved the closest look, because it changes the availability term of the
cost model and the failure mode would be silent: if deferrals never became
misses, the miss rate would be understated. Checked both directions —
a deferred target that expires **is** counted as `EXPIRED`, and a deferred
target that recovers **does** execute without being counted as a miss. Correct.

## The Gate B1 blocker is clearable

v0.1.3 flagged Hypothesis as an unresolved Gate B1 blocker because it could not
be installed in the review environment. It runs here.

```
5 property tests, max_examples raised 60 → 1500, sequences to 90 actions
PASS
```

That is a real property campaign, not a deterministic soak standing in for one.

---

## The finding: a restart laundered every HALT

Found by running **v0.1.3's own auditor against v0.1.3's own soak output**:

```
INVARIANT 7: broker send in HALTED mode        (seeds 11/29/47/63)
```

The natural reading is auditor false positive — the mode fold does not reset on
`PROCESS_STARTED`, so an old HALT appears to persist across a restart.

It was not a false positive.

```
after HALT:      mode = HALTED,  trades = 0
after RESTART:   mode = NORMAL,  trades = 1     <-- every version, v0.1.0 through v0.1.3
```

A HALT is a durable statement: *we found something we cannot explain, stop.*
Clearing it on restart makes restarting a way to bypass a safety stop. The
dangerous path is also the likely one — watchdog kills the engine, an operator
or a supervisor restarts it, and trading resumes with the root cause
undiagnosed.

The RUNBOOK already said "HALT is correct behaviour, do not restart to clear
it." ADR-004 already made restart manual for exactly this reason. Nothing
enforced either. **Documentation is not a control.**

### Fix — invariant 22

`restore_from_journal` scans for the last HALT and any subsequent
`HALT_ACKNOWLEDGED`. An unacknowledged HALT brings the engine up HALTED, and it
outranks the residual path so a HALTED system is never quietly downgraded to
FLATTEN_ONLY (which still permits closing orders).

Clearing requires a named operator and a written resolution, both journalled:

```bash
python -m ib_execution.ack_halt --journal data/journal.db --show
python -m ib_execution.ack_halt --journal data/journal.db \
    --operator olivia --resolution "manual TWS order, cancelled, account verified flat"
```

The tool refuses without attribution, prints the events leading to the HALT
before offering to clear anything, and is never invoked automatically.

**The controller was changed to match the auditor, not the reverse.** That
direction matters: a detector relaxed to fit current behaviour stops being a
detector. Invariant 22 is now audited, and `halt` is in the generated action set
so no sequence can trade its way out of a HALT.

---

## Verification

```
suite                 121 tests pass (was 111)
property campaign     max_examples 1500, sequences to 90 actions, PASS
randomized soak       5 seeds x 200 trials x 60 actions, restarts and halts
                      1639 orders, 1415 executions
                      0 audit findings   (v0.1.3 had 21 across the same seeds)
                      0 moments with 2+ live orders
                      0 exceptions
demo, preflight       PASS
```

---

## Freeze recommendation

**Freeze the spec and this implementation as the Phase 0 baseline.** Gate B1 is
not passed and should not be claimed; what is now true is that the property
campaign runs, the auditor covers 19 of 22 invariants, and the remaining B1 work
is the process-level kill tests and bridge failure coverage v0.1.3 already
listed.

Still outstanding and unchanged: `DO NOT CONNECT TO IB PAPER OR LIVE YET`.

And still the highest-value work available: **Gate A needs none of this.**
