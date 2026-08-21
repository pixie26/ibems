# Gate B1 exact-freeze owner acceptance

This attestation binds the technical evidence and owner risk acceptance to one
exact tested freeze. It is not an authorization to send Paper or Live orders.

## 1. Frozen commit and evidence identity

| Field | Value |
|---|---|
| `commit_sha` | e90c1f4b464c83898c036055b27e17f7eb0da0eb |
| `worktree_clean` | `true` |
| `source_tree_sha256` | fa255c266f29c1704c30140313119fa051a1d622b74fa1502b8f8576d7ee3189 |
| `config_tree_sha256` | 2afcb80305ec6de198a911624c05f6e7681326d16d53fe0a268e60594ab26dcf |
| `dependency_lock_sha256` | 6156296dd9b10927a0700cc2dfd77d42a19a6c39c0bb2753d982b30045de1a5b |
| `resolved_environment_sha256` | 93913643b16d269e7ba91cbf212d717c060966053b53ffed8fb60b03e0ea8835 |
| Python | 3.12.3 |
| Platform | Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 |
| Freeze campaign run | 32440196821 |
| Freeze artifact digest | sha256:3f2c6cad37d5368b1b25e664ce9b8886d28a4a18bb9f98eccea51d04b138fbe9 |
| Evidence snapshot sha256 | 5f32829f2b48263fe9631ad988ca820796325a87cc5d7bf19146d9de5f6f3fc4 |

The formal and storage manifests agree on the commit, source tree, dependency
lock and resolved environment. The durable evidence snapshot embeds the raw
manifests and compact transcripts so this decision does not depend on the
90-day GitHub artifact retention period.

## 2. Gate requirements

| # | Requirement | Technical evidence |
|---|---|---|
| B1.0 | reproducible environment | exact lock and resolved-environment hash in both manifests |
| B1.1 | single-writer process ownership | deterministic process-lock and force-kill coverage |
| B1.2 | calendar fail-closed | deterministic calendar coverage |
| B1.3a | fatal host exit and supervisor policy | fatal-fence, supervisor and real-storage fatal exits |
| B1.3b | durable fatal fence | repaired-journal restart refusal and real-fault fences |
| B1.4 | real storage faults | disk-full, WAL corruption and real dm-delay fsync stall |
| B1.5 | exact-freeze owner risk acceptance | this document and durable evidence snapshot |
| B1.6 | out-of-band journal witness | witness tests and real WAL rollback/forced crossing |

### B1.4 real-storage detail

| Field | Value |
|---|---|
| Journal volume (device / size) | `/mnt/ibems-freeze-journal`; st_dev 49; 268435456 bytes free before drill |
| Fence volume (device) | `/mnt/ibems-freeze-fence`; st_dev 50, distinct from journal volume |
| `disk_full` exit code | `10` |
| `disk_full` fence raised | `true` |
| `wal_corruption` rollback observed | 139 committed events discarded above witnessed seq 6500; witness not crossed in natural rollback |
| Forced witness crossing exit code | `15` |
| Forced witness crossing fence raised | `true` |
| `fsync_stall` mechanism | real block-layer `dm-delay` |
| Healthy delay / observed broker writes | 200 ms / 1 healthy-control FakeBroker write |
| Stalling delay / journal timeout | 45000 ms / 30.0 seconds |
| `fsync_stall` exit code | `10` |
| Broker writes after fsync fault | `0` |
| Storage manifest `passed` | `true` |
| Storage manifest `inconclusive` | `[]` |

## 3. Formal campaign

| Stage | Result | Artifact sha256 / reference |
|---|---|---|
| deterministic suite | PASS | 5a9986fba5167fe583267412620028fdce0d82a20f55972dc275bb46c9211363 |
| generated tests, default profile | PASS, 5 tests | 4d2df7a96ce4a667da3ccf295f2d16c728a65ed0e861a3baa919f7bb469b02b5 |
| generated tests, gate profile | PASS, two campaigns each 1500 passing / 0 failing | b83b84cae29d0219adbeba476c61d744c9f3129381caef87540cf66dcef16135 |
| Hypothesis version | 6.165.2 | embedded formal manifest |
| Hypothesis seed | 20260809 | embedded formal manifest |
| subprocess force-kill windows | PASS, 7 tests | 83a1593c5b5783693fe3006d1f6597bcb4f3a5968f23b59481ee09a0477f910a |
| deterministic lifecycle soak | PASS, 20 seeds x 50 actions | 93788211ea80de574e36279f05e2a4d46351ac6cd6b547ce2569afa5cda6beb0 |
| journal auditor over soak journals | PASS | same deterministic-soak transcript |
| real disk-full | PASS; exit 10 and durable fence | embedded storage manifest |
| real WAL corruption | PASS; measured rollback plus forced witness crossing | embedded storage manifest |
| real dm-delay fsync stall | PASS; exit 10, fence, zero post-fault writes | embedded storage manifest |

## 4. Safety-invariant scope

The campaign covers the known B1 invariants through property tests, runtime
controls and/or auditor evidence as applicable: single-writer ownership;
idempotent decision, intent and execution identity; durable-before-send;
write-state and operating-mode gates; cancel/replace and restart safety;
unknown-fact HALT; risk caps and configuration identity; callback failure;
overnight survivability; startup must-reject checks; and durable HALT across
restart. This coverage is not a proof that the invariant list is complete.

Real-IB portions of restart reconciliation, real broker identity/facts and
callback behavior remain outside B1 and are explicitly deferred to B2.

## 5. Scope limits accepted by the owner

- B1 broker behavior uses `FakeBroker`; real IB protocol behavior is not claimed.
- One-host/process ownership is not a distributed or network-filesystem lock.
- Real storage/fault evidence is Linux-only. Windows `msvcrt.locking`, volume
  identity, NTFS ENOSPC/stall behavior and the Windows service wrapper were not
  exercised with real failures.
- The fsync drill uses real block-layer `dm-delay`, but its constrained
  filesystem is tmpfs-backed rather than persistent physical media.
- No complete Full-RTH Recorder session is claimed by Gate B1.
- Windows order-capable deployment requires production-OS validation before it
  can be considered, independently of this B1 acceptance.

## 6. Owner risk acceptance

| Field | Value |
|---|---|
| Owner | 我 |
| Accepted at (UTC) | 2026-08-21T03:44:42Z |
| B1 scope acceptance | `ACCEPT` |
| Overnight risk acceptance | `ACCEPT` |
| Accepted max_position_shares | `5` |
| Recorded overnight_gap_stress_pct | `0.15` |
| Recorded max_overnight_loss | `500` |
| Windows gap acceptance | `ACCEPT` |
| Real IB scope | `DEFER_TO_B2` |
| Additional B1-level hazard identified | `NO` |
| Decision | `PASS` |
| Notes | Owner accepts at most 5 shares overnight if flattening is impossible; 15% stress and $500 loss budget are recorded model assumptions. Linux-only storage-fault evidence is accepted for B1, with Windows validation still mandatory before any order-capable deployment. This decision permits B2 read-only/paper protocol validation only and does not authorize an order. |

## 7. Attestation boundary

The attestation commit may contain only this sign-off, the exact-freeze durable
evidence snapshot and regenerated `STATE.json`. Any behavior, configuration,
dependency, test or campaign-script change requires a new freeze campaign.
