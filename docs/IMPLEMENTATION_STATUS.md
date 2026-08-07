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
- **2026-08-07 复测**（paper 账户 `DUN921978`，NetLiq $1,000,000，0 持仓 / 0 挂单）：IB `10089` entitlement 阻塞已解除，`marketDataType=1`（Live），`entitlement_blocked=false`，时钟偏差约 +1.4s；20 秒采样收到 BidAsk tick 与 4 条 5 秒 realtime bars，但 `AllLast` 一路 0 tick，preflight 三路 sample 全非零条件未满足，`passed=false`（证据 `artifacts/ib_preflight/20260807T151722Z/report.json`）。

## Recorder 行情状态（2026-08-07 更新）

- 历史阻塞 `10089`（缺 `SPY ARCA/TOP/ALL` API LIVE entitlement）**已解除**：`marketDataType=1`，`entitlement_blocked=false`。
- 新观察到的待解释项：同一次预检中 `AllLast` tick-by-tick 一路在 20 秒窗内 0 tick，BidAsk 与 5s bars 两路正常。需在正常 RTH 内复测，确认是采样窗口内的 tick 稀疏还是 `AllLast` 订阅路径问题。
- 在三路 sample 全部稳定非零、且至少有一个完整 Full-RTH health report 之前，Recorder 仍按 fail-closed 退出码 2 处理。

## Gate B1 blockers

- `fatal_shutdown_requested` 已形成 core contract，但真实 execution-engine 宿主退出码/监督器尚未集成；
- OS/卷级 disk-full 与真实 WAL 损坏演练尚未替代确定性 fault injection；
- `docs/INVARIANT_COVERAGE.md` 尚需正式评审签字。

## Recorder deployment blockers

- 未发现 `config/paper.yml`；
- ~~`SPY ARCA/TOP/ALL` API LIVE entitlement 缺失（IB `10089`）~~ —— **2026-08-07 已解除**，`marketDataType=1`、`entitlement_blocked=false`；
- 2026-08-07 预检中 `AllLast` 一路 0 tick（BidAsk / 5s bars 正常），需在 RTH 内复测确认原因；
- 独立 recorder username/Gateway 与首个 Full-RTH health report 尚未验证。

Recorder 可以在 Gate A/B1 之外独立上线，但只能保持 Read-Only API；它的通过不推导 trading adapter 可连接。

## Gate B2 blockers

- `IbAdapter.place_order/cancel_order` 和完整 callback/error mapping；
- 静态账户的三轮 stable-snapshot 候选读取已观察；仍缺成交/回调并发、Gateway restart、1101/1102 和 late callback 下的 barrier 实测；
- orderRef/permId/clientId、1100/1101/1102、fee delay/correction/cancel race/Gateway restart 的 documented-vs-observed matrix；
- 人工 1–5 股 paper target/cancel（只能在 B1 正式通过后）。
