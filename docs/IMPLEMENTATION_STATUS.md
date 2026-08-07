# Implementation status — v0.1.5.dev0

## 冻结标签

```text
Phase 0 reviewed baseline
Specification frozen
Gate B1 not passed
DO NOT CONNECT THE TRADING ADAPTER TO IB PAPER OR LIVE
```

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

## Gate B1 blockers（7 项，机器可读状态见 `STATE.json`）

- **B1.0** reproducible environment：Python 3.12.x + `uv.lock` + 实际 environment hash 三者同时进 manifest；
- **B1.1** single-writer process ownership：`journal.db` 跨进程独占锁；第二个进程非零退出且不连接 broker；
- **B1.2** calendar fail-closed：`SUPPORTED_YEARS` 之外拒绝启动（当前只覆盖 2026）；
- **B1.3a** fatal host exit：`fatal_shutdown_requested` → execution host 非零退出；生产 supervisor 配置进签字 artifact；
- **B1.3b** durable fatal fence：带外持久围栏；存储修复后用健康 journal 重启仍拒绝进入 NORMAL；
- **B1.4** real storage faults：真实受限卷 disk-full / WAL 损坏 / fsync stall；
- **B1.5** independent exact-commit sign-off：22 条不变量 + 全部 artifact 绑定 exact commit。

`docs/INVARIANT_COVERAGE.md` 的 P/R/A 三层齐备不等于 Gate B1 通过；上述任一项未完成都必须维持 `gate_b1: NOT_PASSED`。

## Recorder deployment blockers

- 未发现 `config/paper.yml`；
- ~~`SPY ARCA/TOP/ALL` API LIVE entitlement 缺失（IB `10089`）~~ —— **2026-08-07 已解除**，`marketDataType=1`、`entitlement_blocked=false`；
- `AllLast` 订阅路径尚无有效观测（2026-08-07 的计数口径无效），改成 event-driven 累积计数后才能重测；
- 独立 recorder username/Gateway 与首个 Full-RTH health report 尚未验证。

Recorder 可以在 Gate A/B1 之外独立上线，但只能保持 Read-Only API；它的通过不推导 trading adapter 可连接。

## Gate B2 blockers

- `IbAdapter.place_order/cancel_order` 和完整 callback/error mapping；
- 静态账户的三轮 stable-snapshot 候选读取已观察；仍缺成交/回调并发、Gateway restart、1101/1102 和 late callback 下的 barrier 实测；
- orderRef/permId/clientId、1100/1101/1102、fee delay/correction/cancel race/Gateway restart 的 documented-vs-observed matrix；
- 人工 1–5 股 paper target/cancel（只能在 B1 正式通过后）。
