# Implementation status — v0.1.5.dev0

> 本文主体是 Phase 0/B1 工程过程记录，部分冻结标签和 B2 blocker 描述保留了当时语境。当前权威的人类可读状态见 [`GATE_B2_STATUS_20260810_ZH.md`](GATE_B2_STATUS_20260810_ZH.md)：Gate B1 已在 commit `117188cea539...` PASS；Gate B2 为 `READ-ONLY IN PROGRESS`，尚未 PASS，也未授权订单。

## 历史 Phase 0 冻结标签

```text
Phase 0 reviewed baseline
Specification frozen
Gate B1 not passed
DO NOT CONNECT THE TRADING ADAPTER TO IB PAPER OR LIVE
```

上面的代码块是该工程记录创建时的历史标签，不是当前 Gate 判定。当前标签为：

```text
Gate B1 PASS recorded for exact-freeze commit 117188cea539...
STATE.json: gate_b1_attested_freeze = 117188cea539...
STATE.json: gate_b1_covers_worktree = false for current B2 HEAD
Gate B2 READ-ONLY IN PROGRESS; NOT PASS
NO PAPER-ORDER OR LIVE-ORDER AUTHORIZATION
```

两项分别从历史 Git attestation blob 与当前 `attestation.validate` 派生；当前树不被 B1
attestation 覆盖不会抹掉历史 exact-freeze PASS，也不会放宽当前树覆盖规则。实现与边界见
[`GATE_B2_REVIEW_20260810.md`](GATE_B2_REVIEW_20260810.md) §4。

## 已实现并在 Python 3.12.13 验证

- event/state model、SQLite WAL journal、decision/exec 原子幂等；
- target-position controller、exact broker identity、cancel → stable reconcile → replace；
- unstable snapshot 不得恢复 `SYNCED`；
- durable HALT + exact-cause CAS acknowledgement；connect/reconcile 前强制 journal restore；
- async single-writer bridge、Windows/POSIX watchdog PID identity 与 fencing；
- 22 条 invariant 的 P/R/A auditor 入口，包括 per-intent overnight stress 重算；
- 144 个 non-property tests：PASS；
- 5 个 property tests：默认 100-example profile PASS；
- 当前完整工作树合计 149 tests：PASS；
- 正式 Gate profile：两个生成测试各 1,500 examples PASS，seed `2026080601`，source-tree hash 与 manifest 复算一致；
- 7 个 subprocess force-kill crash windows：PASS；
- SQLite locked、disk full、malformed WAL、fsync timeout、writer death、bridge death：fail-closed tests PASS；
- read-only Full-RTH Recorder：订阅/存储/Parquet/health/hash 代码与本地测试 PASS；
- 4002 Read-Only Gateway 握手：PASS；server time、SPY `conId=756733` 与合约详情读取 PASS；
- positions → all-open-orders → executions 三轮读取连续两对 canonical hash 相等（22/3/0）；这是静态时段的候选屏障证据，不是 Gate B2 通过；
- **2026-08-07 复测**（paper 账户已 redact，0 持仓 / 0 挂单）：IB `10089` entitlement 阻塞已解除，`marketDataType=1`（Live），`entitlement_blocked=false`，报告的时钟偏差约 +1.4s；20 秒采样收到 BidAsk tick 与 4 条 5 秒 realtime bars，`AllLast` 报 0 tick，`passed=false`（证据 `artifacts/ib_preflight/20260807T151722Z/report.json`）。
  - **该次预检的 tick 计数口径已被判定无效**，`AllLast=0` 不构成证据：脚本读的是 `Ticker.tickByTicks` 的残余缓冲，而 `ib_async` 在网络更新之间清空它；`bars_5s` 之所以正确是因为它读的是会累积的 `RealTimeBarList`。
  - **该次预检的时钟偏差同样不可用**：`datetime.now() - reqCurrentTime()` 未做 RTT 补偿，而 IB server time 只有秒级粒度，所以 +1.4s 里有多少是真实偏差无法区分。健康阈值是 2s，按当时口径会因量化噪声误判整天数据。

## Recorder 行情状态（2026-08-07 更新）

- 历史阻塞 `10089`（缺 `SPY ARCA/TOP/ALL` API LIVE entitlement）**已解除**：`marketDataType=1`，`entitlement_blocked=false`。
- `AllLast` 是否正常**目前无证据，不是「有 0 tick 这个证据」**。2026-08-07 的计数口径已被判定无效（见上），因此该次预检对 `AllLast` 既不支持也不否定任何结论。改成 event-driven 累积计数后重测，才第一次会产生关于 `AllLast` 的有效观测。
- 明确不成立的推理：「5s TRADES bar 正常 ⇒ tick-by-tick AllLast 正常」。`reqRealTimeBars` 与 `reqTickByTickData` 是不同的订阅路径，前者健康不构成后者健康的证据。
- 在三路 sample 全部稳定非零、且至少有一个完整 Full-RTH health report 之前，Recorder 仍按 fail-closed 退出码 2 处理。

## Gate B1 blockers（8 项，机器可读状态见 `STATE.json`）

- **B1.0** reproducible environment：Python 3.12.x + `uv.lock` + 实际 environment hash 三者同时进 manifest；
- **B1.1** single-writer process ownership：`journal.db` 跨进程独占锁；第二个进程非零退出且不连接 broker；
- **B1.2** calendar fail-closed：`SUPPORTED_YEARS` 之外拒绝启动（当前只覆盖 2026）；
- **B1.3a** fatal host exit：`fatal_shutdown_requested` → execution host 非零退出；生产 supervisor 配置进签字 artifact；
- **B1.3b** durable fatal fence：带外持久围栏；存储修复后用健康 journal 重启仍拒绝进入 NORMAL；
- **B1.4** real storage faults：真实受限卷 disk-full / WAL 损坏 / fsync stall。**已在 96MB loop-mounted ext4（`-m 0`）上实跑**：
  - `disk_full` **PASS**：真实 ENOSPC → journal 写失败 → 另一卷上的 durable fence 已写入 → host 退出码 10；
  - `wal_corruption` **PASS**（见 B1.6）：真实回滚丢失的事件若在 witness 之上则正确容忍，强制越过 witness 则退出码 15 + fence；
  - `fsync_stall` **INCONCLUSIVE**：本机 FUSE harness 无法承载 SQLite WAL 的 `-shm` mmap（SIGBUS，已实测确认 non-WAL 与 `locking_mode=EXCLUSIVE` 均正常），需在生产主机上用 dm-delay 重跑；
- **B1.5** independent exact-commit sign-off：22 条不变量 + 全部 artifact 绑定 exact commit；
- **B1.6（本次演练新增，已实现）** journal witness：WAL 损坏会**静默丢弃已提交事件**，详见下节。

`docs/INVARIANT_COVERAGE.md` 的 P/R/A 三层齐备不等于 Gate B1 通过；上述任一项未完成都必须维持 `gate_b1: NOT_PASSED`。

## B1.6 — WAL 损坏会静默丢弃已提交事件（2026-08-08 已修）

这是 B1.4 演练在**真实 ext4 卷**上发现的，不是注入故障能发现的。

```text
SIGKILL 引擎，WAL 保留约 4.1 MB
损坏 WAL header 的 checksum（offset 24）
损坏前可回放事件 4,406 → 损坏后 4,379
→ 27 条已提交事件消失，SQLite 未报任何错误，引擎正常启动
```

这些事件全部是 `synchronous=FULL` 下 `commit()` 成功返回的——调用方**已被告知它们持久化了**。WAL header checksum 失败会让恢复流程**丢弃帧而不是报错**，数据库仍内部自洽，只是变短了；`PRAGMA integrity_check` 查不出来，因为确实没有损坏，只是少了行。直接打穿不变量 2。

### 修法：带外见证者，绑定事件身份而不是裸 max_seq

`src/ib_execution/journal_witness.py`。写在 fence 那个卷上（同一个理由：要能活过 journal 所在卷）。**不是每次 commit 都写**——那会为心跳和遥测付一次跨卷 fsync；只在**安全边界**写。

记录的不是「至少有 N 行」，而是授权了这次副作用的**那一条具体证据**：

```text
journal_id + seq + event_type + intent_id + order_ref + payload digest
```

启动时（构造 broker 之前）四种失败都拒绝：seq 缺失、digest 不符、`journal_id` 不符、`max_seq < witness.seq`。`journal_id` 那条顺带挡住恢复的备份和被换掉的 journal 文件——裸序号做不到。

### 覆盖面：先做对抗性演练，再决定，而不是先假设

原先的判断是「只覆盖 broker write 就够，漏掉的都是丢了也不危险的」。**这个判断是错的**，`tests/test_journal_witness.py` 的 HALT tail-loss drill 证伪了它：

```text
seq 100  最后一次 broker-write witness
seq 120  HALTED
崩溃 + WAL 回滚到 seq 110
→ max_seq 110 ≥ 100，witness 通过
→ 但 HALT 没了 → 重启回到 NORMAL → 不变量 22 被存储打穿
```

因此 `SAFETY_CRITICAL_TYPES` 现在也覆盖 `OPERATING_MODE_CHANGED` 与 `HALT_CAUSE_ADDED`。仍然只需一条记录：WAL 恢复截断的是尾部而不是打洞，所以钉住**最新**的安全关键事件就界定了它之前所有事件的丢失。

写入失败的处理是**不对称**的，这是刻意的：

- **broker write 之前** witness 写不了 → 拒绝下单 + fence。此刻「什么都不做」是明确安全的，因为还没发单。
- **HALT 之后** witness 写不了 → 告警但**不撤销 HALT**。HALT 已经发生且已落盘，为了满足见证者去撤销它是荒谬的。

### 真实卷实测（`artifacts/gate_b1_storage/`）

```text
HALT 被 witness 钉在 seq 2309（OPERATING_MODE_CHANGED）
真实 WAL 回滚丢 21 条，全部在 witness 之上
→ 引擎正确启动（只丢遥测不是安全事件；否则每次 WAL 受损都变成停机）
强制越过 witness（删除 seq ≥ 2309）
→ host 退出码 15（EXIT_WITNESS），fence 已升起，原因写明缺了哪一条
```

## Recorder deployment blockers

- 未发现 `config/paper.yml`；
- ~~`SPY ARCA/TOP/ALL` API LIVE entitlement 缺失（IB `10089`）~~ —— **2026-08-07 已解除**，`marketDataType=1`、`entitlement_blocked=false`；
- `AllLast` 订阅路径尚无有效观测（2026-08-07 的计数口径无效），改成 event-driven 累积计数后才能重测；
- 独立 recorder username/Gateway 与首个 Full-RTH health report 尚未验证。

Recorder 可以在 Gate A/B1 之外独立上线，但只能保持 Read-Only API；它的通过不推导 trading adapter 可连接。

## Gate B2 blockers

- `IbAdapter.place_order/cancel_order` 和完整 callback/error mapping；
- 空状态 stable-snapshot、Gateway 正常 restart、Task Manager End task、Windows `TerminateProcess`、API client 异常死亡及 Gateway 存活时的 `1100 -> 1102` 已直接观察；仍缺 1101、非空动态 broker facts、成交/回调并发和 late callback 下的 barrier 实测；
- SPY overnight 和正式 RTH 三路行情尚未完成；休市零 tick 不形成结论；
- orderRef/permId/clientId、fee delay/correction/cancel race 仍未产生真实订单证据；
- 只读证据封存后，才由 owner 单独决定是否授权 1 股 SPY paper-order protocol；当前没有该授权。
