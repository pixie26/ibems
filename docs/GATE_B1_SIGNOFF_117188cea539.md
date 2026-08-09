# Gate B1 exact-freeze owner acceptance

This document records **owner risk acceptance**, not a claim that the owner independently audited every implementation line. Technical claims are bound to the exact-freeze campaign and durable evidence snapshot identified below.

The owner decisions were explicitly provided at `2026-08-09T05:13:10Z`. After that decision, the only change before this final freeze was CI artifact-path hygiene (`.github/workflows/ci.yml`: JUnit output moved under gitignored `artifacts/ci/`). The behavior source tree, risk configuration and dependency lock represented by the hashes below did not change, so the owner decisions are carried forward without expanding their meaning.

## 1. Frozen commit

| Field | Value |
|---|---|
| `commit_sha` | 117188cea53906665739af3775af64d156856f41 |
| `worktree_clean` | true |
| `source_tree_sha256` | 4b7cecb59d21608bb24a7bd85e560ec3af7885f4d4c1b5a7ba0d7ac3305400c8 |
| `config_tree_sha256` | 2afcb80305ec6de198a911624c05f6e7681326d16d53fe0a268e60594ab26dcf |
| `dependency_lock_sha256` | 6156296dd9b10927a0700cc2dfd77d42a19a6c39c0bb2753d982b30045de1a5b |
| `resolved_environment_sha256` | 93913643b16d269e7ba91cbf212d717c060966053b53ffed8fb60b03e0ea8835 |
| Python | 3.12.3 |
| Platform | Linux-6.17.0-1020-azure-x86_64-with-glibc2.39 |
| Freeze campaign run | 31297918125 |
| Freeze artifact digest | sha256:ed9e6220634a8610ed6c9d831a6b290a2ae47466287725542753b11ee04110fd |
| Evidence snapshot sha256 | 90c3694581bf3ef146e615ad4a65d5716e461006e9b78f71bedf047e96656b6e |

GitHub Actions records bind run `31297918125` to the exact freeze SHA above and artifact `gate-b1-freeze-117188cea53906665739af3775af64d156856f41` with the digest above. The durable evidence file was generated from that artifact; its Git blob SHA is `44a4a388300786edd4179006f7f83fc9e23c3544`, matching the blob SHA computed from the local builder output byte-for-byte.

## 2. Gate requirements

| # | Requirement | Exact-freeze technical result |
|---|---|---|
| B1.0 | reproducible environment | PASS — pinned lock, Python 3.12.3, resolved environment hash recorded |
| B1.1 | single-writer process ownership | PASS — process-lock/SIGKILL evidence in deterministic campaign |
| B1.2 | calendar fail-closed | PASS — deterministic calendar coverage |
| B1.3a | fatal host exit + supervisor policy | PASS — fatal-fence/supervisor tests and real fault exits |
| B1.3b | durable fatal fence | PASS — repaired-journal refusal and real storage fences |
| B1.4 | real storage faults | PASS — disk-full, WAL corruption and real dm-delay fsync stall |
| B1.5 | exact-freeze owner risk acceptance | ACCEPTED — section 6 |
| B1.6 | out-of-band journal witness | PASS — normal rollback above witness tolerated; forced crossing refused |

## 3. Formal campaign

| Stage | Result | Reference |
|---|---|---|
| deterministic suite | PASS — 314 tests, 0 failures/errors, 1 skipped | `deterministic.xml` |
| generated tests, default profile | PASS — 5/5 | `property_default.xml` |
| generated tests, gate profile | PASS — 5/5 | `property_gate.xml` |
| core lifecycle property 1 | 1,500 passing / 0 failing | `test_no_sequence_violates_invariants` |
| core lifecycle property 2 | 1,500 passing / 0 failing | `test_never_two_orders_in_flight` |
| Hypothesis version | 6.165.2 | — |
| Hypothesis seed | 20260809 | — |
| subprocess force-kill windows | PASS — 7/7 | `process_crash.txt` |
| deterministic lifecycle soak + auditor | PASS — 20 seeds × 50 actions | `deterministic_soak_auditor.txt` |

## 4. Real storage evidence

| Field | Observed |
|---|---|
| Journal failure domain | `st_dev=49`, constrained 256 MiB |
| Fence failure domain | `st_dev=50`, separate 64 MiB |
| `disk_full` | PASS — real ENOSPC, exit 10, durable fence raised |
| `wal_corruption` | PASS — 8,311 → 8,269 events; 42 committed events discarded, all above witness seq 5,441 |
| Forced witness crossing | PASS — remove from seq 5,441, exit 15, durable fence |
| `fsync_stall` mechanism | real `dm-delay` |
| Healthy control | 200 ms delay, exactly 1 observed `place_order`, process alive, no fence |
| Stalling case | 45,000 ms delay, 30 s journal timeout, exit 10, durable fence |
| Broker writes after fsync fault | 0 |
| Storage manifest | `passed=true`, `inconclusive=[]` |

## 5. Scope limits retained after PASS

- B1 uses `FakeBroker`; it does **not** claim real IB protocol validation.
- Invariant 10 real restart reconciliation, invariant 14 real unknown/ambiguous broker facts and order identity, and invariant 18 real callback/disconnect behavior remain B2.
- All B1 real storage/fault evidence is Linux-only. Windows `msvcrt.locking`, Windows volume-serial failure-domain checks, NTFS ENOSPC/stall behavior and `deploy/ibems-execution-service.ps1` have not had real-fault validation.
- The GitHub-hosted fsync drill uses real block-layer `dm-delay`; the constrained filesystem is tmpfs-backed rather than physical persistent media.
- No complete Full-RTH recorder session is claimed by B1.
- P/R/A coverage proves coverage of the known 23 invariants, not mathematical completeness of the safety model.

## 6. Owner risk acceptance — HUMAN INPUT

| Field | Value |
|---|---|
| Owner | pixie26 (project owner) |
| Accepted at (UTC) | 2026-08-09T05:13:10Z |
| B1 scope acceptance | `ACCEPT` |
| Overnight risk acceptance | `ACCEPT` |
| Accepted max_position_shares | `5` |
| Recorded overnight_gap_stress_pct | `0.15` |
| Recorded max_overnight_loss | `500` |
| Windows gap acceptance | `ACCEPT` |
| Real IB scope | `DEFER_TO_B2` |
| Additional B1-level hazard identified | `NO` |
| Decision | `PASS` |
| Notes | Owner explicitly accepts a maximum SPY position of 5 shares for the overnight-failure case. The 15% stress and $500 loss budget are recorded as current frozen model assumptions, not overclaimed as separately validated human judgements. Windows real-fault coverage is deferred and retained as a documented gap. Real IB behavior will be tested with IB Gateway in B2. PASS authorizes B2 read-only/paper protocol validation only; it does not itself authorize order sending. |

## 7. Transition

This PASS closes Gate B1 for the exact freeze above and permits **Gate B2 read-only / paper protocol validation** to begin. Real IB behavior must be observed rather than inferred from `FakeBroker`. Any behavior, test, config or dependency change after this freeze invalidates this attestation and requires a new freeze.
