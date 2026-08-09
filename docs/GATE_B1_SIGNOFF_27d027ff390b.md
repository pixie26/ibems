# Gate B1 sign-off — exact-freeze review packet

**This file is prefilled only with machine-observed evidence. Reviewer verdicts and the final decision are intentionally blank.**

A sign-off binds to the exact tested freeze commit below. Any behavior/config/dependency/test change requires a new freeze campaign. The later attestation may contain only `STATE.json`, this sign-off, and `docs/GATE_B1_EVIDENCE_27d027ff390b.json`.

---

## 1. Frozen commit

| Field | Value |
|---|---|
| `commit_sha` | 27d027ff390b88695652a0dd0af3ef852d1ba583 |
| `worktree_clean` | true |
| `source_tree_sha256` | 2c525a65e009e0fd87f75eef66db300e503b0ddb223526184d98693943e5b41a |
| `config_tree_sha256` | 2afcb80305ec6de198a911624c05f6e7681326d16d53fe0a268e60594ab26dcf |
| `dependency_lock_sha256` | 6156296dd9b10927a0700cc2dfd77d42a19a6c39c0bb2753d982b30045de1a5b |
| `resolved_environment_sha256` | 93913643b16d269e7ba91cbf212d717c060966053b53ffed8fb60b03e0ea8835 |
| Python | 3.12.3 |
| Platform | Linux-6.17.0-1020-azure-x86_64-with-glibc2.39 |
| Freeze campaign run | 31293680320 |
| Freeze artifact digest | sha256:9d523a554c0a813b1a339ed8506e45b13c91d5ad73c466a3113a179e42d53754 |
| Evidence snapshot sha256 | f7f38ea2cc2bcc7b2d65c7eae67dad30733050e350b52df8fc452e5846ac7ec5 |

Durable evidence: `docs/GATE_B1_EVIDENCE_27d027ff390b.json`.

- formal manifest SHA-256: `2eeaf8a2eefca3e5644503647fde9bb935feea692a20cc2ee3c7d8638a2814a2`
- storage manifest SHA-256: `69acdd24caf40deba0f2cab777e06147b29901536c06a8f287eb134eba147118`
- both manifests bind the exact freeze SHA, source-tree hash, dependency lock and resolved environment above.

## 2. Gate requirements

The evidence column is prefilled; **Reviewer verdict is for the independent reviewer.**

| # | Requirement | Evidence | Reviewer verdict |
|---|---|---|---|
| B1.0 | reproducible environment | exact lock + resolved environment hash; clean formal manifest | |
| B1.1 | single-writer process ownership | deterministic process-lock/SIGKILL coverage | |
| B1.2 | calendar fail-closed | deterministic calendar-coverage tests | |
| B1.3a | fatal host exit + supervisor policy | fatal-fence + supervisor tests; real storage fatal exits | |
| B1.3b | durable fatal fence | repaired-journal restart refusal + real fault fences | |
| B1.4 | real storage faults | disk-full, WAL corruption, real dm-delay fsync stall; storage `passed=true`, `inconclusive=[]` | |
| B1.5 | independent exact-freeze review | this document + durable evidence snapshot; final reviewer decision still required | |
| B1.6 | out-of-band journal witness | witness tests + real WAL rollback/forced crossing | |

### B1.4 real-storage detail

| Field | Observed value |
|---|---|
| Journal volume | separate tmpfs, `st_dev=48`, 256 MiB |
| Fence volume | separate tmpfs, `st_dev=49`, 64 MiB |
| `disk_full` | PASS — real ENOSPC, exit `10`, durable fence raised |
| `wal_corruption` rollback observed | yes — 8,386 → 8,269 events; 117 committed events discarded |
| Witnessed WAL seq | 5,503 |
| Tolerated rollback crossed witness | no; all 117 lost events were above the witnessed safety boundary |
| Forced witness crossing | PASS — removed from seq 5,503; exit `15`, durable fence raised |
| `fsync_stall` mechanism | real block-layer `dm-delay` v1.5.0 |
| Healthy control | 200 ms delay; exactly 1 observed `place_order`; host remained alive; no fence |
| Stalling case | live reload to 45,000 ms after startup; journal timeout 30.0 s |
| `fsync_stall` exit | `10` + durable fence |
| Broker writes after fsync fault | `0` |
| Storage manifest | `passed=true`, `inconclusive=[]` |

### B1.6 witness detail

| Field | Observed / frozen behavior |
|---|---|
| Witness covers | broker writes and safety-critical HALT events |
| Witness binds | `journal_id`, seq, event type, intent id, order ref, payload digest |
| Startup refuses on | missing witnessed seq, digest mismatch, wrong journal id, rollback below witness |
| Witness write failure before broker write | fail closed; fence; no send |
| Forced WAL crossing | startup refused, exit `15`, fence raised |
| HALT tail-loss protection | evidence present; reviewer verdict required below |

## 3. Formal campaign

| Stage | Result | Evidence hash / reference |
|---|---|---|
| deterministic suite | PASS — 304 tests, 303 passed, 1 skipped, 0 failed | `0b9801722b4ca1da7a5e1ab0b1ea4cfd52116b98800eeed248ba44fd83d9fc7d` |
| generated tests, default profile | PASS — 5/5 | `4d2df7a96ce4a667da3ccf295f2d16c728a65ed0e861a3baa919f7bb469b02b5` |
| generated tests, gate profile | PASS — 5/5; two core lifecycle properties each 1,500 passing / 0 failing examples | `ae59cc1cbfdcdc1322f025678cf3ec703c303782ca4edbbe86465e9885ceda26` |
| Hypothesis version | 6.165.2 | — |
| Hypothesis seed | 20260809 | — |
| subprocess force-kill windows | PASS — 7/7 | `83a1593c5b5783693fe3006d1f6597bcb4f3a5968f23b59481ee09a0477f910a` |
| deterministic lifecycle soak | PASS — 20 seeds × 50 actions | `93788211ea80de574e36279f05e2a4d46351ac6cd6b547ce2569afa5cda6beb0` |
| journal auditor over soak journals | PASS | same soak/auditor transcript above |
| real disk-full | PASS | storage manifest SHA `69acdd24…7118` |
| real WAL corruption | PASS | storage manifest SHA `69acdd24…7118` |
| real dm-delay fsync stall | PASS | storage manifest SHA `69acdd24…7118` |

## 4. Invariants

`P/R/A` below reflects the frozen coverage matrix. **The last column is intentionally blank for independent review.** `n/a` for invariant 0 auditor coverage is deliberate; the control is OS process ownership rather than a journal semantic.

| # | Invariant | P | R | A | Reviewer verdict |
|---|---|---|---|---|---|
| 0 | single-writer process ownership | yes | OS exclusive lock + non-zero refusal | n/a | |
| 1 | decision id once | yes | DB PK + atomic accept | yes | |
| 2 | durable before broker write | yes | commit-before-call + witness | yes | |
| 3 | one live intent per leg | yes | state gate | yes | |
| 4 | no second send pending ACK | yes | state gate | yes | |
| 5 | no replacement pending cancel | yes | state gate | yes | |
| 6 | write only CONNECTED+SYNCED | yes | write-boundary assertion | yes | |
| 7 | opening only NORMAL | yes | `_can_write` | yes | |
| 8 | FLATTEN_ONLY only target zero | yes | `_evaluate` | yes | |
| 9 | resolve working before flatten | yes | cancel/reconcile gate | yes | |
| 10 | restart reconcile before send | yes | forced restore before connect/reconcile | yes | |
| 11 | expired target never sent | yes | `_evaluate` | yes | |
| 12 | exec id once / corrections append-only | yes | atomic book transaction | yes | |
| 13 | missing fee is benign | yes | structural | yes | |
| 14 | unknown broker fact halts | yes | exact identity + HALT | yes | |
| 15 | explained residual → FLATTEN_ONLY | yes | restore fold | yes | |
| 16 | order/share/notional/position caps | yes | RiskEngine + restart restore | yes | |
| 17 | intent stores config hash | yes | intent construction | yes | |
| 18 | callback/bridge failure fail-closed | yes | guarded callbacks + bridge liveness | yes | |
| 19 | overnight survivability | yes | numeric stress | yes | |
| 20 | every invariant has P/R/A | yes | coverage contract | yes | |
| 21 | startup must-reject self-test | yes | controller construction + calendar coverage | yes | |
| 22 | restart cannot clear HALT | yes | forced restore + exact CAS ack + durable fence | yes | |

Important scope boundary: invariants 10, 14 and 18 still have **real-IB components deferred to Gate B2**. A B1 verdict must not be read as validating real IB reconciliation, observed permId behavior, or real callback handlers.

## 5. Scope limits and accepted residual risk

The following are part of the claim being reviewed:

- B1 broker behavior uses `FakeBroker`. Real IB protocol behavior remains Gate B2.
- Invariants 10, 14 and 18 retain real-broker components for B2: IB restart reconciliation, observed `permId` behavior, and real callback handlers.
- Invariant 0 is one-host/process ownership on a shared local filesystem, not a distributed lock. Network filesystems are outside scope.
- **All real B1 storage/fault evidence is Linux-only.** Windows-specific `msvcrt.locking`, Windows volume-serial failure-domain checks, NTFS ENOSPC/stall behavior, and `deploy/ibems-execution-service.ps1` have not been exercised with real failures.
- **Owner decision:** the Windows gap is recorded and accepted as a **non-blocker for B2 read-only / paper progression**. This does **not** authorize order-capable Windows deployment. Production-OS validation is required before relying on Windows-specific controls for orders.
- The fsync drill uses real block-layer `dm-delay`, but the GitHub-hosted constrained filesystem is tmpfs-backed rather than persistent physical media.
- No complete Full-RTH recorder session is claimed by Gate B1.

## 6. Independent decision — TO BE COMPLETED BY REVIEWER

| Field | Value |
|---|---|
| Reviewer | |
| Reviewed at (UTC) | |
| Decision | |
| Notes | |

A `PASS` permits **Gate B2 read-only protocol validation only**. It does **not** authorize an order, paper or live.

The reviewer should also fill the `Reviewer verdict` column for B1.0–B1.6 and invariants 0–22 above. If any item cannot be supported from the durable evidence and frozen source, the final Decision is `FAIL` and no attestation should be finalized.
