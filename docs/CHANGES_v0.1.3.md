# v0.1.3.dev0 — final Phase 0 review of v0.1.2

## Safety fixes

- EOD flatten now durably installs target zero before cancelling a working order.
- Target replacement resets the reprice ladder; only timeout repricing increments it.
- Every clean cancel now forces a fresh broker snapshot/reconciliation before replacement sizing.
- Added non-terminal `CANCEL_REJECTED`; if the order remains working after reconcile, HALT rather than auto-retry.
- Late executions update in-memory position and force reconciliation when local state was terminal.
- Order callbacks can no longer promote account-wide sync; only reconciliation can.
- Added EOD completion lifecycle and preserved working exposure in residual records.
- EOD residual recording never downgrades HALTED.
- Watchdog refuses to kill when process start identity is unavailable or mismatched.
- Added `TARGET_DEFERRED`; transient blocks are not counted as misses until terminal outcome.
- Reconciliation now converges retained, unexpired latest targets.
- Tightened risk config validation and expanded invariant-16 audit to shares/notional.

## Correctness and packaging

- Changed project version from invalid `0.1.2-phase0` to PEP 440 `0.1.3.dev0`.
- Added v0.1.3 lifecycle regressions, unsafe-config cases and auditor false-positive tests.
- Added dependency-free deterministic randomized soak script.
- Updated review, final execution plan, status, coverage and validation documents.

## Safety status

```text
Gate B1: NOT PASSED
IB Paper/Live: DO NOT CONNECT
```
