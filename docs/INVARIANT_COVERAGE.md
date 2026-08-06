# Invariant coverage matrix — v0.1.5.dev0 Phase 0 review

`COMPLETE` requires all three reviewed layers:

- **P**: property/adversarial test;
- **R**: runtime assertion or structural enforcement;
- **A**: offline journal auditor.

A deterministic test is not automatically a process-crash proof. The package remains **not Gate B1 complete**.

| # | Short description | P | R | A | Current status / next evidence |
|---|---|---:|---:|---:|---|
| 1 | decision id once | yes | atomic DB constraint | yes | COMPLETE for current process model |
| 2 | durable before broker write | partial | structural ordering | yes | add real subprocess kill after WAL/before send |
| 3 | one live intent per leg | yes | state gate | yes | generated restart cases present; rerun with Hypothesis |
| 4 | no second send pending ack | yes | state gate | yes | COMPLETE for current event model |
| 5 | no replacement pending cancel | yes | state gate | yes | COMPLETE for current event model |
| 6 | write only CONNECTED+SYNCED | yes | write-boundary `_require` | yes | COMPLETE for controller writes |
| 7 | opening only NORMAL | yes | `_can_write` | yes | COMPLETE |
| 8 | FLATTEN_ONLY only target zero | yes | `_evaluate` | yes | COMPLETE |
| 9 | resolve working before flatten | yes | zero-target convergence + cancel gate | yes | clean cancel now forces reconciliation |
| 10 | restart reconcile before send | partial | sync starts UNVERIFIED; unstable snapshot cannot sync | yes | add real process restart fixture and prove real IB snapshot barrier |
| 11 | expired target never sent | yes | `_evaluate` | yes | COMPLETE |
| 12 | exec id once/corrections append-only | partial | atomic DB transaction | yes | add transaction/process crash test |
| 13 | missing fee is benign | yes | structural | yes | auditor uses word-boundary match; no `verify` false positive |
| 14 | unknown broker fact halts | yes | exact identity + durable HALT cause | yes | add explicit permId mismatch matrix in Gate B2 |
| 15 | explained residual -> FLATTEN_ONLY | yes | restore path | yes | includes position=0 + working exposure |
| 16 | order/share/notional caps | yes | RiskEngine + restart restore | yes | auditor must receive the actual frozen session limits |
| 17 | intent stores risk config hash | yes | intent constructor | yes | COMPLETE |
| 18 | callback exception fail-closed | yes | `_guarded` | yes | adapter/bridge integration still unverified |
| 19 | overnight survivability | yes | numeric stress check | no | persist applied stress inputs/result, audit against frozen config |
| 20 | every invariant has P/R/A | meta | no | no | **NOT COMPLETE — blocks Gate B1; matrix now covers all 22 rows** |
| 21 | startup must-reject self-test | yes | preflight | partial | event emitted; audit ordering before engine start/send |
| 22 | restart cannot clear HALT; exact acknowledgement only | yes | atomic latest-cause CAS; no in-process resume | yes | COMPLETE for current process model; real process restart fixture still supports invariant 10 |

## Gate B1 exit rule

1. no row may remain `partial` or `no`;
2. Hypothesis-generated lifecycle sequences must run;
3. every generated journal must pass the auditor;
4. process-level crash fixtures must cover durable-before-send and execution-booking windows;
5. README/manifest may claim Gate B1 only after this matrix is reviewed COMPLETE.
