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
- SPY 行情预检明确失败：IB `10089`，API LIVE entitlement 缺失；Recorder 1.6 秒内 fail-fast、退出码 2，并生成 unhealthy Parquet/health/manifest。

## Gate B1 blockers

- `fatal_shutdown_requested` 已形成 core contract，但真实 execution-engine 宿主退出码/监督器尚未集成；
- OS/卷级 disk-full 与真实 WAL 损坏演练尚未替代确定性 fault injection；
- `docs/INVARIANT_COVERAGE.md` 尚需正式评审签字。

## Recorder deployment blockers

- 未发现 `config/paper.yml`；
- 当前 4002 Gateway 已验证可只读连接，但 `SPY ARCA/TOP/ALL` API LIVE entitlement 缺失；
- 独立 recorder username/Gateway 与首个 Full-RTH health report 尚未验证。

Recorder 可以在 Gate A/B1 之外独立上线，但只能保持 Read-Only API；它的通过不推导 trading adapter 可连接。

## Gate B2 blockers

- `IbAdapter.place_order/cancel_order` 和完整 callback/error mapping；
- 静态账户的三轮 stable-snapshot 候选读取已观察；仍缺成交/回调并发、Gateway restart、1101/1102 和 late callback 下的 barrier 实测；
- orderRef/permId/clientId、1100/1101/1102、fee delay/correction/cancel race/Gateway restart 的 documented-vs-observed matrix；
- 人工 1–5 股 paper target/cancel（只能在 B1 正式通过后）。
