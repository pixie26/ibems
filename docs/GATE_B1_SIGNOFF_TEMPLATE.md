# Gate B1 sign-off

> Copy this file to `docs/GATE_B1_SIGNOFF_<first-12-chars-of-freeze-sha>.md`.
> Fill every evidence field from the artifacts generated for that exact freeze.
> Do not copy values from a previous sign-off.

**A sign-off binds to one exact tested freeze commit.** If code, config,
dependencies or tests change, take a new freeze and rerun the complete campaign.
The later commit that records the signature is an **attestation commit**, not a
new tested freeze. It may contain only `STATE.json`, this sign-off document and
the exact-freeze durable evidence snapshot. `tests/test_provenance.py` enforces
that restriction.

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
dependency lock and resolved environment. The original GitHub Actions artifact
is useful but expires; before signing, build and commit
`docs/GATE_B1_EVIDENCE_<freeze[:12]>.json` with
`scripts/build_gate_b1_evidence.py`. The sign-off records that file's SHA-256 so
the reviewed evidence remains reconstructable after the Actions artifact
expires.

## 2. Gate requirements

These ids are defined by `ib_execution.gate.B1_REQUIREMENTS`; CI checks this
template contains exactly the same set.

| # | Blocker | Artifact | Verdict |
|---|---|---|---|
| B1.0 | reproducible environment | formal campaign manifest | |
| B1.1 | single-writer process ownership | deterministic JUnit + process-lock tests | |
| B1.2 | calendar fail-closed | deterministic JUnit :: calendar coverage | |
| B1.3a | fatal host exit + supervisor policy | deterministic JUnit :: fatal fence + supervisor | |
| B1.3b | durable fatal fence | deterministic JUnit :: repaired journal still refuses | |
| B1.4 | real storage faults | storage manifest: disk-full + WAL corruption + fsync stall | |
| B1.5 | independent exact-freeze review and this signed document | sign-off + durable evidence snapshot | |
| B1.6 | out-of-band witness that committed events still exist | witness tests + WAL crossing evidence | |

### B1.4 real-storage detail

Injected exceptions are not sufficient evidence. Review the actual constrained
filesystem campaign and record the observed results.

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

### B1.6 witness detail

SQLite WAL recovery can discard a committed tail after later WAL damage while
leaving an internally valid, shorter database. The reviewer must verify that
the out-of-band witness pins both broker-write boundaries and safety-critical
HALT events.

| Field | Value |
|---|---|
| Witness covers | broker writes and safety-critical HALT events |
| Witness binds | `journal_id`, `seq`, event type, intent id, order ref, payload digest |
| Startup refuses on | missing seq, digest mismatch, wrong journal id, rollback below witness |
| Witness write failure before broker write | must fence and not send |
| Forced WAL crossing | must refuse startup / exit 15 and raise fence |
| HALT tail-loss protection | reviewer verdict |

## 3. Formal campaign

| Stage | Result | Artifact sha256 / reference |
|---|---|---|
| deterministic suite | | |
| generated tests, default profile | | |
| generated tests, gate profile (≥1,500 examples each) | | |
| Hypothesis version | | — |
| Hypothesis seed | | — |
| subprocess force-kill windows | | |
| deterministic lifecycle soak | | |
| journal auditor over soak journals | | |
| real disk-full | | |
| real WAL corruption | | |
| real dm-delay fsync stall | | |

## 4. Invariants

Each row is reviewed independently. `P`, `R`, and `A` mean property test,
runtime assertion/control, and auditor/evidence respectively. Presence of all
three is not itself a PASS; the reviewer checks that the evidence actually
proves the invariant stated.

| # | Invariant | P | R | A | Reviewer verdict |
|---|---|---|---|---|---|
| 0 | single-writer process ownership | | | | |
| 1 | decision id once | | | | |
| 2 | durable before broker write | | | | |
| 3 | one live intent per leg | | | | |
| 4 | no second send pending ACK | | | | |
| 5 | no replacement pending cancel | | | | |
| 6 | write only CONNECTED+SYNCED | | | | |
| 7 | opening only NORMAL | | | | |
| 8 | FLATTEN_ONLY only target zero | | | | |
| 9 | resolve working before flatten | | | | |
| 10 | restart reconcile before send | | | | |
| 11 | expired target never sent | | | | |
| 12 | exec id once / corrections append-only | | | | |
| 13 | missing fee is benign | | | | |
| 14 | unknown broker fact halts | | | | |
| 15 | explained residual → FLATTEN_ONLY | | | | |
| 16 | order/share/notional/position caps | | | | |
| 17 | intent stores config hash | | | | |
| 18 | callback/bridge failure fail-closed | | | | |
| 19 | overnight survivability | | | | |
| 20 | every invariant has P/R/A | | | | |
| 21 | startup must-reject self-test | | | | |
| 22 | restart cannot clear HALT | | | | |

## 5. Scope limits and accepted residual risk

A B1 PASS is deliberately narrower than “safe to trade”. These limits are part
of the signed claim, not footnotes.

- The trading IB adapter remains unverified; B1 uses `FakeBroker` for broker
  behaviour. Gate B2 exists to replace documentation assumptions with observed
  IB protocol behaviour.
- Invariants 10, 14 and 18 retain real-broker components for B2: restart
  reconciliation against IB, observed `permId` behaviour, and real callback
  handlers.
- Invariant 0 is one-host/process ownership on a shared local filesystem, not a
  distributed lock. Network filesystems are outside the frozen architecture.
- **All B1 real-storage/fault evidence is Linux-only.** The Windows-specific
  `msvcrt.locking` ownership path, Windows volume-serial failure-domain checks,
  NTFS ENOSPC/stall behaviour and `deploy/ibems-execution-service.ps1` have not
  been exercised with real failures.
- **Owner decision for this gate:** the Windows gap above is recorded and
  accepted as a **non-blocker for B2 read-only / paper progression**. It does
  not authorize order-capable Windows deployment; production-OS validation is
  required before relying on those Windows-specific controls for orders.
- The GitHub-hosted fsync drill uses real block-layer `dm-delay`, but its
  constrained filesystem is tmpfs-backed rather than persistent physical
  media.
- Recorder cross-stream diagnostics are measurements, not execution safety
  authorization; no complete Full-RTH recorder session is claimed by B1.
- Known historical repository exposures or other accepted limitations must be
  stated explicitly in reviewer notes.

## 6. Independent decision

| Field | Value |
|---|---|
| Reviewer | |
| Reviewed at (UTC) | |
| Decision | `PASS` / `FAIL` |
| Notes | |

The reviewer must be independent of the implementation being signed. A `PASS`
permits **B2 read-only protocol validation to begin**; it does not permit an
order.

## 7. Attestation procedure after a PASS

Start from the exact tested freeze commit at `HEAD`.

First build the durable evidence snapshot from the downloaded workflow artifact:

```bash
python scripts/build_gate_b1_evidence.py \
  --artifact-zip <gate-b1-freeze.zip> \
  --freeze-commit <full-40-char-freeze-sha> \
  --run-id <workflow-run-id> \
  --artifact-name <artifact-name> \
  --artifact-digest sha256:<artifact-digest>
```

Copy the printed evidence snapshot SHA-256 into this sign-off, together with the
exact workflow run and artifact digest. Complete the reviewer decision, then run:

```bash
python scripts/finalize_gate_b1.py --freeze-commit <full-40-char-freeze-sha>
```

The finalizer validates the sign-off, evidence snapshot, exact freeze and dirty
worktree, then calls provenance regeneration. It does **not** manually set PASS;
`STATE.json` must re-derive `gate_b1=PASS` and `signed_off_commit=<freeze>`.
Commit **only**:

```text
STATE.json
docs/GATE_B1_SIGNOFF_<first-12-chars-of-freeze-sha>.md
docs/GATE_B1_EVIDENCE_<first-12-chars-of-freeze-sha>.json
```

That new commit is the attestation commit. CI verifies that the signed freeze is
its ancestor, the diff contains only those metadata files, the evidence SHA is
correct, and a later `python -m ib_execution.provenance` regeneration preserves
PASS. Any source/test/script/config/dependency change invalidates the old PASS
and requires a new freeze campaign.
