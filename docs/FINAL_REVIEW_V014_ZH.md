# v0.1.4 Phase 0 最终审查

## 结论

`v0.1.4` 修复“重启清除 HALT”是正确且必要的，但不能原样冻结。审查发现两个
load-bearing 问题，并已在 `v0.1.5.dev0` 修复。

## 问题一：HALT acknowledgement 不是精确确认

旧实现没有把确认动作原子绑定到当前最新 HALT cause。并发或旧页面可能用旧确认
清掉后来出现的新故障；系统已经 HALTED 时，第二个原因也可能不落盘。另一个问题是
确认后当前 controller 直接回到 NORMAL，绕过人工停止、重启和 reconciliation。

优化：

- 新原因使用 `HALT_CAUSE_ADDED` durable event；
- acknowledgement 使用 latest-cause sequence 的 SQLite transaction CAS；
- stale acknowledgement 必须失败；
- restart 只恢复内存状态，不嵌套生成新 HALT；
- acknowledgement 后当前进程仍 HALTED。

## 问题二：reconciliation snapshot 不天然原子

随机生命周期测试复现：旧单在 snapshot 中看似已不存在，系统发出 replacement，随后
旧单的延迟 execution 到达，账户持仓超过 `max_position`。根因不是 delta 公式，而是把
一次非原子的 positions/open-orders/executions 组合读取当作 broker truth。

优化：

- `BrokerSnapshot` 增加必填 `is_stable`；
- 不稳定 snapshot 永远不能恢复 `SYNCED`；
- FakeBroker 在有待处理 position/order callback 时返回 unstable；
- Gate B2 必须实测 IB 的 completion/watermark barrier。

## 其他意见

当前最主要风险已经不是缺功能，而是设计继续膨胀。v0.1.5 后停止增加 Phase 0 功能，
只完成 Gate B1 证据闭环：Hypothesis campaign、真实进程 kill、22 条 P/R/A 和 auditor。
