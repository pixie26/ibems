# Invariant coverage matrix — v0.1.5.dev0

`COMPLETE` 要求三层同时存在：Property/adversarial test（P）、runtime assertion/structural enforcement（R）、offline journal auditor（A）。

**P/R/A 齐备不推导 `gate_b1: PASS`。** 正式 campaign、8 项 B1 blocker 和独立评审签字都是独立的退出条件。权威状态只有一个来源：仓库根目录的 `STATE.json`（由 `python -m ib_execution.provenance` 生成，`tests/test_provenance.py` 强制它与工作树一致）。本文件不复述该状态。

| # | 简述 | P | R | A | 当前证据 |
|---|---|---|---|---|---|
| 0 | single-writer process ownership | yes | OS 独占锁 + 非零退出 | n/a | 真实双进程 + SIGKILL 后继承 |
| 1 | decision id once | yes | DB PK + atomic accept | yes | complete |
| 2 | durable before broker write | yes | commit-before-call + 带外 witness | yes | subprocess force-kill；真实卷 WAL 回滚越过 witness → 退出码 15 |
| 3 | one live intent per leg | yes | state gate | yes | generated restart lifecycle |
| 4 | no second send pending ACK | yes | state gate | yes | generated lifecycle |
| 5 | no replacement pending cancel | yes | state gate | yes | cancel crash window |
| 6 | write only CONNECTED+SYNCED | yes | write-boundary assertion | yes | complete |
| 7 | opening only NORMAL | yes | `_can_write` | yes | complete |
| 8 | FLATTEN_ONLY only target zero | yes | `_evaluate` | yes | complete |
| 9 | resolve working before flatten | yes | cancel/reconcile gate | yes | complete |
| 10 | restart reconcile before send | yes | forced restore before connect/reconcile | yes | real IB barrier remains Gate B2 |
| 11 | expired target never sent | yes | `_evaluate` | yes | complete |
| 12 | exec id once/corrections append-only | yes | atomic book transaction | yes | partial-fill force-kill window |
| 13 | missing fee is benign | yes | structural | yes | complete |
| 14 | unknown broker fact halts | yes | exact identity + HALT | yes | observed permId matrix remains B2 |
| 15 | explained residual -> FLATTEN_ONLY | yes | restore fold | yes | complete |
| 16 | order/share/notional/position caps | yes | RiskEngine + restart restore | yes | evidence carries frozen limits |
| 17 | intent stores config hash | yes | intent construction | yes | complete |
| 18 | callback/bridge failure fail-closed | yes | guarded callbacks + bridge liveness | yes | real adapter handlers remain B2 |
| 19 | overnight survivability | yes | numeric stress | yes | auditor recomputes per-intent evidence |
| 20 | every invariant has P/R/A | yes | coverage contract | yes | auditor fails on missing row |
| 21 | startup must-reject self-test | yes | Controller 构造路径 + calendar coverage | yes | 配置 hash 与日历覆盖必须先于 start/intent |
| 22 | restart cannot clear HALT | yes | forced restore + exact CAS ack + durable fence | yes | subprocess kill 后重启；存储修复后仍拒绝 |

### 不变量 0 与 21、22 的关系

三条一起才构成完整的 fail-closed，缺一条就有缺口：

- **22** 保护**已经落盘**的 HALT。journal 本身失效时，HALT 落不了盘，所以 22 不会被触发——不是被绕过，是被跳过。
- **B1.6 的 witness** 是同一族的第四块：22 假设「落盘了就还在」，而 WAL 恢复会**丢弃已提交帧而不报错**。对抗性演练证明只钉 broker write 不够——HALT 不是 broker write，丢掉它就绕过了 22——所以 witness 也覆盖 HALT 类安全事件。
- **0** 保证同一时刻只有一个进程能写，否则 1–4 全部只在单进程内成立。
- **B1.3b 的 durable fence** 补上 22 够不到的那一段：把「这次决定停」带过进程边界、重启和存储恢复。

## Gate B1 的 8 项 blocker

权威状态在 `STATE.json`；本文件不复述。`READY_FOR_FREEZE` 只表示机制可以冻结并重跑，仍不等于独立 reviewer 已签字。

| Blocker | 内容 | 证据位置 |
|---|---|---|
| B1.0 | reproducible environment | `uv.lock` + formal campaign manifest 的 `resolved_environment_sha256` |
| B1.1 | single-writer process ownership | `tests/test_journal_ownership.py`（真实双进程 + SIGKILL） |
| B1.2 | calendar fail-closed | `tests/test_calendar_coverage.py` |
| B1.3a | fatal host exit | `tests/test_fatal_fence.py` + `deploy/` + `tests/test_supervisor.py` |
| B1.3b | durable fatal fence | `tests/test_fatal_fence.py::test_a_repaired_journal_still_refuses_to_trade` |
| B1.4 | real storage faults | unified freeze campaign：`disk_full`、`wal_corruption`、真实 `dm-delay fsync_stall` 均要求 PASS，且 `inconclusive=[]` |
| B1.5 | independent exact-freeze sign-off | `docs/GATE_B1_SIGNOFF_TEMPLATE.md` + `scripts/finalize_gate_b1.py` + attestation CI |
| B1.6 | journal witness | 绑定 `journal_id`+seq+event identity+digest；覆盖 broker write 与 HALT 类事件；`tests/test_journal_witness.py` + WAL crossing drill |

### Freeze 与签字的 commit 语义

被完整 campaign 测试的是 **freeze commit**。独立 reviewer 在该 commit 的 artifact 上完成审阅后，允许生成一个后继 **attestation commit**，但它只能包含：

- `STATE.json`
- `docs/GATE_B1_SIGNOFF_<freeze-sha[:12]>.md`

原因是 Git commit 不可能把“包含自己的 commit SHA”写进自己。要求 signed commit 必须等于包含签字文件的 HEAD 会形成不可满足的自引用。新的 provenance test 会验证 freeze 是 attestation 的祖先，并拒绝两者之间任何行为代码、测试、配置或依赖变化。

Gate B2 的 IB stable-snapshot、permId、1101/1102 和 callback observed matrix 不倒灌进 B1，也不因 B1 的 FakeBroker 证据而预判通过。
