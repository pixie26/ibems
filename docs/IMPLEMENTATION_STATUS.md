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
- 138 个 non-property tests：PASS；
- 5 个 property tests：默认 100-example profile PASS；
- 正式 Gate profile：两个生成测试各 1,500 examples PASS，seed `2026080601`，source-tree hash 与 manifest 复算一致；
- 7 个 subprocess force-kill crash windows：PASS；
- SQLite locked、disk full、malformed WAL、fsync timeout、writer death、bridge death：fail-closed tests PASS；
- read-only Full-RTH Recorder：订阅/存储/Parquet/health/hash 代码与本地测试 PASS。

## Gate B1 blockers

- `fatal_shutdown_requested` 已形成 core contract，但真实 execution-engine 宿主退出码/监督器尚未集成；
- OS/卷级 disk-full 与真实 WAL 损坏演练尚未替代确定性 fault injection；
- `docs/INVARIANT_COVERAGE.md` 尚需正式评审签字。

## Recorder deployment blockers

- 本机未发现 4002/7497 Gateway/TWS listener；
- 未发现 `config/paper.yml`；
- SPY tick-by-tick LIVE entitlement、独立 paper username/Gateway 与首个 Full-RTH health report 未验证。

Recorder 可以在 Gate A/B1 之外独立上线，但只能保持 Read-Only API；它的通过不推导 trading adapter 可连接。

## Gate B2 blockers

- `IbAdapter.place_order/cancel_order` 和完整 callback/error mapping；
- positions/open orders/executions 的 observed stable-snapshot protocol；
- orderRef/permId/clientId、1100/1101/1102、fee delay/correction/cancel race/Gateway restart 的 documented-vs-observed matrix；
- 人工 1–5 股 paper target/cancel（只能在 B1 正式通过后）。
