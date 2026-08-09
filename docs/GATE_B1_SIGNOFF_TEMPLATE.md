# Gate B1 exact-freeze owner acceptance template

> Copy this file to `docs/GATE_B1_SIGNOFF_<first-12-chars-of-freeze-sha>.md`.
> Fill evidence fields only from the artifacts generated for that exact freeze.
> The human section records **owner risk acceptance**, not a claim that the owner
> independently audited every implementation line.

A successful attestation binds to one exact tested freeze commit. If behavior,
config, dependencies or tests change, take a new freeze and rerun the complete
campaign. The later commit that records the owner decision is an attestation
commit, not a new tested freeze. It may contain only `STATE.json`, this sign-off
and the exact-freeze durable evidence snapshot.

---

## 1. Frozen commit

| Field | Value |
|---|---|
| `commit_sha` | |
| `worktree_clean` | must be `true` |
| `source_tree_sha256` | |
| `config_tree_sha256` | |
| `dependency_lock_sha256` | |
| `resolved_environment_sha256` | |
| Python | must be 3.12.x |
| Platform | |
| Freeze campaign run | |
| Freeze artifact digest | |
| Evidence snapshot sha256 | |

The formal and storage manifests must agree on the exact commit, source tree,
dependency lock and resolved environment. GitHub Actions artifacts expire, so
before attestation build and commit
`docs/GATE_B1_EVIDENCE_<freeze[:12]>.json` with
`scripts/build_gate_b1_evidence.py`.

## 2. Gate requirements

These ids are defined by `ib_execution.gate.B1_REQUIREMENTS`; CI checks this
template contains exactly the same set. Their technical evidence is generated
by the exact-freeze campaign. The human decision is recorded separately in
section 6.

| # | Requirement | Technical evidence |
|---|---|---|
| B1.0 | reproducible environment | formal campaign manifest + exact lock + resolved environment hash |
| B1.1 | single-writer process ownership | deterministic process-lock/SIGKILL coverage |
| B1.2 | calendar fail-closed | deterministic calendar coverage |
| B1.3a | fatal host exit + supervisor policy | fatal-fence + supervisor tests + real storage fatal exits |
| B1.3b | durable fatal fence | repaired-journal restart refusal + real fault fences |
| B1.4 | real storage faults | disk-full + WAL corruption + real dm-delay fsync stall |
| B1.5 | exact-freeze owner risk acceptance | this document + durable evidence snapshot + derived attestation |
| B1.6 | out-of-band journal witness | witness tests + real WAL rollback / forced crossing |

### B1.4 real-storage detail

| Field | Value |
|---|---|
| Journal volume (device / size) | |
| Fence volume (device) | must differ from journal volume |
| `disk_full` exit code | expect `10` |
| `disk_full` fence raised | |
| `wal_corruption` rollback observed | |
| Forced witness crossing exit code | expect `15` |
| Forced witness crossing fence raised | |
| `fsync_stall` mechanism | expect real `dm-delay` |
| Healthy delay / observed broker writes | |
| Stalling delay / journal timeout | |
| `fsync_stall` exit code | expect `10` |
| Broker writes after fsync fault | must be `0` |
| Storage manifest `passed` | must be `true` |
| Storage manifest `inconclusive` | must be `[]` |

## 3. Formal campaign

| Stage | Result | Artifact sha256 / reference |
|---|---|---|
| deterministic suite | | |
| generated tests, default profile | | |
| generated tests, gate profile (>=1,500 examples each) | | |
| Hypothesis version | | — |
| Hypothesis seed | | — |
| subprocess force-kill windows | | |
| deterministic lifecycle soak | | |
| journal auditor over soak journals | | |
| real disk-full | | |
| real WAL corruption | | |
| real dm-delay fsync stall | | |

## 4. Safety invariants — technical coverage, not a completeness proof

`P`, `R`, and `A` mean property test, runtime assertion/control, and
auditor/evidence. The matrix proves coverage of the **known** invariants; it does
not prove this list is complete. Section 6 therefore requires the owner to state
whether any additional B1-level hazard is currently known.

| # | Invariant | P | R | A | Scope note |
|---|---|---|---|---|---|
| 0 | single-writer process ownership | yes | OS exclusive lock + non-zero refusal | n/a | Windows implementation not real-fault tested |
| 1 | decision id once | yes | DB PK + atomic accept | yes | B1 core |
| 2 | durable before broker write | yes | commit-before-call + witness | yes | B1 core |
| 3 | one live intent per leg | yes | state gate | yes | B1 core |
| 4 | no second send pending ACK | yes | state gate | yes | B1 core |
| 5 | no replacement pending cancel | yes | state gate | yes | B1 core |
| 6 | write only CONNECTED+SYNCED | yes | write-boundary assertion | yes | B1 core |
| 7 | opening only NORMAL | yes | `_can_write` | yes | B1 core |
| 8 | FLATTEN_ONLY only target zero | yes | `_evaluate` | yes | B1 core |
| 9 | resolve working before flatten | yes | cancel/reconcile gate | yes | B1 core |
| 10 | restart reconcile before send | yes | forced restore before connect/reconcile | yes | **real IB reconciliation deferred to B2** |
| 11 | expired target never sent | yes | `_evaluate` | yes | B1 core |
| 12 | exec id once / corrections append-only | yes | atomic book transaction | yes | B1 core |
| 13 | missing fee is benign | yes | structural | yes | B1 core |
| 14 | unknown broker fact halts | yes | exact identity + HALT | yes | **real IB broker facts deferred to B2** |
| 15 | explained residual -> FLATTEN_ONLY | yes | restore fold | yes | B1 core |
| 16 | order/share/notional/position caps | yes | RiskEngine + restart restore | yes | B1 core |
| 17 | intent stores config hash | yes | intent construction | yes | B1 core |
| 18 | callback/bridge failure fail-closed | yes | guarded callbacks + bridge liveness | yes | **real IB callbacks deferred to B2** |
| 19 | overnight survivability | yes | numeric stress | yes | mechanism tested; **5-share risk acceptance is human** |
| 20 | every invariant has P/R/A | yes | coverage contract | yes | does not prove list completeness |
| 21 | startup must-reject self-test | yes | controller construction + calendar coverage | yes | B1 core |
| 22 | restart cannot clear HALT | yes | forced restore + exact CAS ack + durable fence | yes | B1 core |

## 5. Scope limits that the owner must understand

- B1 broker behavior uses `FakeBroker`. Real IB protocol behavior is not claimed.
- Invariants 10, 14 and 18 retain real-broker components for B2: IB restart
  reconciliation, observed order/permId behavior, unknown/ambiguous real broker
  facts, disconnect/reconnect and real callback handlers.
- Invariant 0 is one-host/process ownership on a shared local filesystem, not a
  distributed lock. Network filesystems are outside scope.
- All real B1 storage/fault evidence is Linux-only. Windows-specific
  `msvcrt.locking`, Windows volume-serial failure-domain checks, NTFS
  ENOSPC/stall behavior and `deploy/ibems-execution-service.ps1` have not been
  exercised with real failures.
- The GitHub-hosted fsync drill uses real block-layer `dm-delay`, but the
  constrained filesystem is tmpfs-backed rather than persistent physical media.
- No complete Full-RTH recorder session is claimed by B1.

## 6. Owner risk acceptance — HUMAN INPUT

The fields below are the actual human decision. They are deliberately narrow:
the owner is accepting residual risk and the transition to B2, not certifying
that every code path is bug-free.

| Field | Value |
|---|---|
| Owner | |
| Accepted at (UTC) | |
| B1 scope acceptance | `ACCEPT` / `REJECT` |
| Overnight risk acceptance | `ACCEPT` / `REJECT` |
| Accepted max_position_shares | copy exact frozen value |
| Recorded overnight_gap_stress_pct | copy exact frozen value |
| Recorded max_overnight_loss | copy exact frozen value |
| Windows gap acceptance | `ACCEPT` / `REJECT` |
| Real IB scope | `DEFER_TO_B2` / `REJECT` |
| Additional B1-level hazard identified | `NO` / describe blocker |
| Decision | `PASS` / `STOP` |
| Notes | |

Interpretation of the required positive decision:

- `B1 scope acceptance=ACCEPT`: B1 core evidence is sufficient to move to the
  next protocol-validation phase; this is not authorization to send an order.
- `Overnight risk acceptance=ACCEPT` + `Accepted max_position_shares`: the owner
  explicitly accepts the frozen maximum position if the system cannot flatten
  and must hold overnight. The current 15% stress and $500 loss budget are
  recorded alongside that decision as model assumptions; recording them does
  **not** overstate that the owner separately validated those model choices.
- `Windows gap acceptance=ACCEPT`: Linux-only fault evidence does not block B2,
  but Windows order-capable deployment still requires production-OS validation.
- `Real IB scope=DEFER_TO_B2`: FakeBroker evidence is not read as coverage of
  invariants 10/14/18 against real IB; those observations are B2 work.
- `Additional B1-level hazard identified=NO`: after considering known failure
  classes, the owner is not currently aware of another B1-level safety hole.
- `Decision=PASS`: closes B1 and permits **B2 read-only / paper protocol
  validation**. It does not authorize a paper or live order by itself.

## 7. Attestation procedure

Start from the exact tested freeze commit at `HEAD`.

Build the durable evidence snapshot from the downloaded workflow artifact:

```bash
python scripts/build_gate_b1_evidence.py \
  --artifact-zip <gate-b1-freeze.zip> \
  --freeze-commit <full-40-char-freeze-sha> \
  --run-id <workflow-run-id> \
  --artifact-name <artifact-name> \
  --artifact-digest sha256:<artifact-digest>
```

Complete section 6, then run:

```bash
python scripts/finalize_gate_b1.py --freeze-commit <full-40-char-freeze-sha>
```

The finalizer validates the owner decisions, exact frozen risk parameters,
evidence snapshot, exact freeze and dirty worktree, then calls provenance
regeneration. It does not manually set the gate status.

Commit only:

```text
STATE.json
docs/GATE_B1_SIGNOFF_<first-12-chars-of-freeze-sha>.md
docs/GATE_B1_EVIDENCE_<first-12-chars-of-freeze-sha>.json
```

Any source/test/script/config/dependency change invalidates the old attestation
and requires a new freeze campaign.
