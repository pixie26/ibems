# v0.1.2 最终审查意见

日期：2026-08-06  
审查输出：`v0.1.3.dev0-phase0-reviewed`

## 一、结论

v0.1.2 相比上一版有真实进展：restart 已进入生成序列，EOD residual 能落盘，watchdog 增加 PID reuse 防护，auditor 覆盖扩大，风险配置也开始 fail-closed。

但原始 v0.1.2 **不能直接冻结为 Gate B1，更不能连接 IB Paper/Live**。本轮复现出若干会改变持仓或污染成本账本的生命周期错误。它们不是代码风格问题，而是状态语义问题。

修订后的结论：

```text
设计规格：可以冻结
IB-free core：reviewed prototype
Gate B1：未通过
IB Paper/Live：禁止连接
```

## 二、本轮发现并修复的实质问题

### 1. EOD flatten 在存在 working order 时可能永远不发 target=0

原路径先 cancel working order，但没有先保存 zero target。cancel 完成后控制器重新评估的仍是旧 target；进入 `FLATTEN_ONLY` 后旧 target 被拒绝，而 `_check_eod` 又因 `flatten_reason` 已设置而跳过，最终没有真正平仓。

修复：先 durable 接收 zero target，再通过正常 target convergence 路径 cancel → terminal → zero order。

### 2. target change 错误继承 reprice attempt

原 `on_cancelled()` 不区分取消原因，一律增加 attempt。于是新 target、EOD flatten 也会继承旧订单的追价档位。

修复：仅 `reprice_timeout` 增加 attempt；`target_changed` 与 flatten 重置为 attempt 0。

### 3. cancel confirmation 后未重新拉取 broker truth

收到 clean cancel callback 后原代码直接重算并发送 replacement，违反冻结规则 `cancel → terminal → reconcile → recompute`。若成交发生在 cancel race window，replacement 可能按旧 position 发出。

修复：clean cancel 后先进入 `TERMINAL_UNRECONCILED + UNVERIFIED`，强制 fresh snapshot/reconciliation；只有成功后才重新评估 target。

### 4. cancel reject 被错误记成订单终态

原代码用 `ORDER_REJECTED` 记录 cancel reject。该事件在 journal replay 中属于终态，因此仍在 broker working 的自有订单会被移出 open-intent 集合，下一次 reconcile 反而把它判成外来订单。

修复：新增非终态 `CANCEL_REJECTED`，保留 durable ownership并强制 reconcile；若订单仍 working，HALT 等人工确认，不自动重试。

### 5. 延迟 execution 可入账但不更新内存仓位

若 execution 在本地订单已经 terminal 后才到达，journal 会记录成交，但 `_leg_for_ref()` 找不到 live leg，内存 position 保持旧值。下一张 target 可能按错误仓位 sizing。

修复：先按 durable intent 定位 leg 并更新 position；若该 execution 属于本地已终结 intent，则强制 `TERMINAL_UNRECONCILED + UNVERIFIED`。

### 6. 单个 order callback 可错误恢复账户级 sync

原 `on_working()` 会把全局 `sync_state` 提升为 `SYNCED`。1101 后收到一张订单的 working callback，并不能证明 positions、orders、executions、行情订阅都已同步。

修复：只有完整 reconciliation 可以把全局 sync 提升到 `SYNCED`。

### 7. EOD lifecycle 不闭环

原实现没有可靠发出 `EOD_FLATTEN_COMPLETED`，`flatten_reason` 可能跨日残留；position=0 但 working order 的残余也可能不被持久化；记录 residual 时还可能把 `HALTED` 降级为 `FLATTEN_ONLY`。

修复：加入 completion 事件、清理 flatten cause、持久化 working exposure，并禁止任何 EOD 记录动作降低 HALTED。

### 8. watchdog 在无法证明 PID 身份时仍可能 kill

仅有 PID、没有进程 start ticks 时，旧 PID 可能已被系统复用。

修复：recorded/current process identity 任一不可得即拒绝 kill；watchdog 仍然不得重启、下单或修改 mode。

### 9. transient block 被错误计入 miss rate

断线或未同步时，target 可能稍后在有效期内成功执行。立即写 `DECISION_MISSED` 会夸大 Phase 5 availability cost。

修复：先写 `TARGET_DEFERRED`；只有过期、明确风险拒绝、broker 拒绝或 reprice exhaustion 才写最终 miss。成本分析应将最后一次 defer 原因与最终 miss 联结。

### 10. 重连后 retained target 未自动收敛

断线期间收到的新 target 会保存在内存，但 reconcile 完成后原实现不重新评估，系统可能一直保留旧 broker order。

修复：reconcile 成功后重新评估仍有效的 latest desired target；过期 target 永不补发。

### 11. 风险配置与 auditor 仍有缺口

修复：

- 非正数 limits、非布尔 `allow_short`、越过 hard bound 的配置拒绝启动；
- auditor 的 invariant 16 同时检查每日订单数、股数和名义金额；
- flatten-before-working auditor 改为按 `(strategy, symbol)` 检查，避免跨 leg 误报；
- startup self-test 成功写入 durable event。

### 12. Python package 版本不合法

`0.1.2-phase0` 不符合 PEP 440，editable install 失败。

修复为 `0.1.3.dev0`，并已实际验证 `pip install -e . --no-build-isolation --no-deps`。

## 三、验证结果

当前 review 环境：Python 3.13.5。

```text
106 deterministic pytest cases: PASS
5 Hypothesis-gated tests: PRESENT, NOT RUN
python -m compileall src tests scripts: PASS
python scripts/demo.py: PASS
python scripts/deterministic_soak.py --seeds 20 --actions 50: PASS
editable install: PASS
```

Hypothesis 未运行的原因是当前环境没有该包，且内部安装源无可用版本。正式 Gate B1 必须在开发环境执行它，不能用 deterministic soak 替代。

## 四、仍然阻塞 Gate B1 的事项

1. 21 条不变量的 P/R/A 覆盖矩阵仍未全部 COMPLETE；
2. 真正的进程级 kill-after-WAL / kill-after-send 测试尚未完成；
3. engine main/process lifecycle 尚未实现；
4. journal writer 与 async bridge 的进程级失败测试不足；
5. auditor 19/20/21 的证据协议仍需冻结；
6. controller 已约 1,474 行，是维护热点，但 Gate B1 前不建议无证据重构；
7. IB adapter、真实 callback mapping、stable snapshot barrier、recorder subscription、emergency flatten broker calls 全未验证。

## 五、最终意见

v0.1.2 的优化方向正确，但它仍是一个 **Phase 0 reviewed prototype**。本轮修订解决了可复现的关键生命周期错误，适合作为下一轮 Gate B1 的基线；不适合用“测试全绿”包装成可连接 IB 的系统。

下一步应同时推进：

```text
Track A：现有数据成本模型 + bootstrap，先做 SPY economic gate
Track R：full-RTH recorder，尽早积累不可回溯的尾部日数据
Track B：只补 Gate B1 的证据缺口，不扩展功能
```
