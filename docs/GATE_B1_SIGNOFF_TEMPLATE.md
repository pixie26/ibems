# Gate B1 sign-off

> Copy to `docs/GATE_B1_SIGNOFF_<commit-sha-short>.md`, fill in every field
> from generated artifacts, and commit it. Do not fill anything in by reading
> a previous sign-off.

**A sign-off binds to one exact commit.** Any change to code, config,
dependencies or tests after the freeze invalidates it — there is no
cherry-picking a small fix and keeping the old proof. If a bug is found during
the campaign, fix it, take a new freeze commit, and rerun the whole campaign.

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

Every value above comes from `artifacts/gate_b1/<stamp>/manifest.json`. The
last two hashes are not redundant: the lockfile says what *should* be
installed, the resolved environment says what *was*, and Gate B1 is an
argument about observations.

## 2. Blockers

The rows below are the ids in `ib_execution.gate.B1_REQUIREMENTS`, and
`tests/test_provenance.py` fails if this table and that registry disagree.

| # | Blocker | Artifact | Verdict |
|---|---|---|---|
| B1.0 | reproducible environment | `artifacts/gate_b1/<stamp>/manifest.json` | |
| B1.1 | single-writer process ownership | `deterministic.xml` :: `test_journal_ownership` | |
| B1.2 | calendar fail-closed | `deterministic.xml` :: `test_calendar_coverage` | |
| B1.3a | fatal host exit + supervisor policy | `deterministic.xml` :: `test_fatal_fence`, `test_supervisor` | |
| B1.3b | durable fatal fence | `deterministic.xml` :: `test_a_repaired_journal_still_refuses_to_trade` | |
| B1.4 | real storage faults | `artifacts/gate_b1_storage/<stamp>/manifest.json` | |
| B1.5 | this document, signed | — | |
| B1.6 | out-of-band witness that committed events still exist | `deterministic.xml` :: `test_journal_witness`, `artifacts/gate_b1_storage/<stamp>/manifest.json` | |

### B1.6 detail

`commit()` returning success under `synchronous=FULL` does not mean the event
is still there after a crash: WAL recovery discards frames whose checksums do
not verify, leaving a database that is internally consistent and simply
shorter. Measured on a real volume: **27 of 4,406 committed events gone, no
error reported, engine started normally.**

| Field | Value |
|---|---|
| Witness covers | every broker write (`place_order`, `cancel_order`) |
| Witness binds | `journal_id`, `seq`, event type, intent id, order ref, payload digest |
| Startup refuses on | missing seq, digest mismatch, wrong `journal_id`, `max_seq < witness.seq` |
| Witness write failure before a broker write | must fence; must not send |
| HALT tail-loss drill | does broker-write-only coverage still satisfy invariant 22? |

The HALT drill is the open question, not a formality. `HALT` and
`HALT_CAUSE_ADDED` are not broker writes, so a broker-write-only witness does
not pin them; if a WAL rollback can drop a HALT while leaving `max_seq` above
the witness, invariant 22 breaks and the witness has to cover safety-critical
events too. Record the answer here rather than assuming it.

### B1.4 detail

Injected faults prove the handler is correct; only the operating system
actually refusing proves the handler is reached.

| Field | Value |
|---|---|
| Journal volume (device, size) | |
| Fence volume (device) | must differ from the journal volume |
| `disk_full` exit code | expect `10` (`EXIT_FATAL_SHUTDOWN`) |
| `disk_full` fence raised | |
| `wal_corruption` exit code | expect `10` or `12` |
| Broker writes after the fault | must be `0` |
| Fault → fence latency | |

## 3. Campaign

| Stage | Result | Artifact sha256 |
|---|---|---|
| deterministic suite | | |
| generated tests, default profile | | |
| generated tests, gate profile (≥1,500 examples each) | | |
| Hypothesis seed | | — |
| subprocess force-kill windows | | |
| journal auditor over campaign journals | | |

The seed is only meaningful alongside the exact Hypothesis version — seeds do
not reproduce across versions, which is why B1.0 is a blocker and not a
nice-to-have.

## 4. Invariants

Each row is signed independently. `COMPLETE` in
`docs/INVARIANT_COVERAGE.md` means P/R/A entries exist; it is not a verdict.

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

## 5. Scope, stated as limits

Sign-off is a claim about what was tested. Record what was not, so nobody
later mistakes silence for coverage.

- The trading IB adapter is **unverified**. Every behavioural claim in
  `ib_adapter.py` comes from IB documentation, not measurement. Gate B2 exists
  to replace those assumptions, and B1 evidence is entirely `FakeBroker`.
- Invariants 10, 14 and 18 have real-broker components that remain B2:
  restart reconciliation against IB, the observed `permId` matrix, and real
  adapter callback handlers.
- Ownership (invariant 0) is mutual exclusion between processes on one host
  sharing one filesystem. It is not a distributed lock, and ADR-001 assumes a
  single host. Network filesystems implement neither lock backend reliably.
- The cross-stream recorder diagnostics are observations, not a gate: the
  bar/tick transform is uncalibrated.
- Known and accepted: a paper account identifier appears in git history at
  commit `15e8000`. History was deliberately not rewritten — the exposure
  cannot be recalled from a public repository, and a rewrite would change
  every commit sha a sign-off binds to.

## 6. Decision

| Field | Value |
|---|---|
| Reviewer | |
| Reviewed at (UTC) | |
| Decision | `PASS` / `FAIL` |
| Notes | |

A `PASS` here permits Gate B2 to begin. **It does not permit an order.** B2's
first phase is read-only: connect, server clock, positions, open orders,
executions, dynamic stable snapshot, 1100/1101/1102, Gateway restart, late
callbacks — producing `DOCUMENTED_VS_OBSERVED.md`. Only after that does a
one-share paper target become defensible.

After signing, set `gate_status.gate_b1` and `gate_status.signed_off_commit`
in `STATE.json` and regenerate:

```bash
python -m ib_execution.provenance
```

`tests/test_provenance.py` asserts that a `PASS` names a commit and that the
commit is HEAD, so the claim cannot outlive the tree it was made about.
