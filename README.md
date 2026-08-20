# IB 执行与风控平台

`ib-execution-platform` 是一个面向单账户、单正常写入者的 IBKR 策略执行与风控平台。策略只提交目标仓位，平台负责风险检查、订单生命周期、持久化、重启恢复和经纪商状态对账。

```text
策略 / 信号 → TargetPosition → 风险引擎 → OMS → IBKR
```

> **安全状态：禁止把交易 Adapter 连接到 IB Paper 或 Live。**
>
> **Gate B1 已在 exact-freeze commit `117188cea539...` 正式 PASS；这不证明真实 IB Gateway 行为，也不授权订单。Gate B2 当前为 `READ-ONLY IN PROGRESS`，尚未 PASS。** 当前工作树包含 B1 freeze 之后的 B2 只读测试改动，因此不能用 B1 attestation 为这些新改动背书。最新状态、证据边界和下一步见 [Gate B2 当前状态摘要](docs/GATE_B2_STATUS_20260810_ZH.md)。

核心原则只有一句：**安全优先于可用性；无法证明状态可信时就停止。**

## 当前状态

| 模块 | 状态 |
|---|---|
| 无 IB 依赖的执行核心 | Gate B1 已在 exact-freeze commit `117188cea539...` 完成正式 campaign、真实存储故障证据和 owner acceptance，结论为 PASS。当前 B2 工作树已有 freeze 后改动，必须单独验证，不能沿用 B1 attestation。 |
| Hypothesis Gate campaign | B1 exact-freeze campaign 已通过；详细计数与 artifact digest 见 `docs/GATE_B1_SIGNOFF_117188cea539.md`。任何 B2 行为代码改动都需要绑定新的 tree 重新验证。 |
| 不变量 0 + 22 条安全不变量 | B1 exact-freeze 的 Property / Runtime / Auditor 与 B1.6 journal witness 已闭环。真实 IB reconciliation、unknown broker facts 和 callback 行为明确留给 B2，不属于 B1 PASS 的证明范围。 |
| 只读 SPY Recorder / B2 preflight | Gateway 4002、server time、SPY `conId=756733`、account summary、空状态 broker snapshot、多 client、Gateway restart / `TerminateProcess` 已有直接观测。`OVERNIGHT` 和 `RTH+SMART` bounded Recorder 已 PASS；持订阅断网已观察 1100→1102。2026-08-12 Full-RTH 因错误 full reconnect / `10197` 提前 FAIL。2026-08-14 已保留 2,645,388 行完整收盘产物，原 v3 health 永久保持 FAIL；同一 immutable raw 的 production-finalizer replay 已通过资源/语义验收，create-only v4 reanalysis 为 `health_ok=true`。目标 Windows lifecycle 证据与下一次端到端 Full-RTH 尚未完成，因此仍不能称为 Full-RTH PASS。1101 仍未观察。 |
| 交易型 IB Adapter | 未授权连接下单路径。`placeOrder`、`cancelOrder`、订单身份、完整 callback/error mapping 和非空动态 reconciliation 尚未在真实 Gateway 验证。 |
| Emergency flatten broker path | 未实现。现有代码只覆盖计划生成与人工确认边界。 |

详细状态与可复查证据：

- **[`STATE.json`](STATE.json) —— 唯一权威的机器可读状态**（gate 状态、source/config/lock 三个树 hash）
- [Gate B2 当前状态摘要](docs/GATE_B2_STATUS_20260810_ZH.md)（当前真实 Gateway 只读测试状态、证据索引和下一步）
- [Gate B2 只读详细证据](docs/GATE_B2_READONLY_20260809.md)
- [Gate B2 SPY OVERNIGHT 行情与 Recorder 证据](docs/GATE_B2_OVERNIGHT_20260810.md)
- [Gate B2 SPY RTH 行情与 Recorder 证据](docs/GATE_B2_RTH_20260810.md)
- [2026-08-12 Full-RTH 提前终止事故报告](docs/INCIDENT_FULL_RTH_20260812_ZH.md)
- [IB documented-vs-observed 矩阵](docs/DOCUMENTED_VS_OBSERVED.md)
- [Gate B2 exact-tree freeze 实施计划](docs/GATE_B2_EXACT_TREE_FREEZE_PLAN_20260820_ZH.md)
- [Gate B2 只读 evidence schema 与 F2 实体审计](docs/GATE_B2_READ_ONLY_EVIDENCE_SCHEMA_V1_ZH.md)
- [实施状态](docs/IMPLEMENTATION_STATUS.md)
- [22 条不变量覆盖矩阵](docs/INVARIANT_COVERAGE.md)
- [审查与执行结论](docs/REVIEW_AND_EXECUTION_20260806_ZH.md)

### provenance 由测试强制，不由纪律强制

此前仓库同时维护 `SHA256SUMS`（手工）和四份互相复述验证状态的散文文档。它们漂移了：README 写 entitlement 已解除、`VALIDATION_MANIFEST.txt` 仍写 FAIL，而 `SHA256SUMS` 对不上自己的工作树。对普通项目这是文档缺陷；**对一个以可审计性为产品的平台，一份描述不了自身工作树的 provenance 文件比没有更糟——它制造信心。**

因此两份手工文件都已删除，取而代之：

```bash
python -m ib_execution.provenance           # 重新生成 STATE.json
python -m ib_execution.provenance --check   # CI：与工作树不一致则失败
```

`tests/test_provenance.py` 另外强制：不得重新引入 `SHA256SUMS`；追踪文件中不得出现账户标识或凭据（文档占位符需在同行写 `provenance-allow: <理由>`）；直接依赖必须 `==` 精确 pin；散文不得声称 Gate B1 已通过而 `STATE.json` 不同意。

Gate campaign 的产物记录四个独立 hash，回答四个不同问题：`source_tree`（跑了什么逻辑）、`config_tree`（在什么限额下跑）、`dependency_lock`（**应该**装什么）、`resolved_environment`（**实际**装了什么）。最后一个才是观测，而 Gate B1 是一个关于观测的论证。

## 为什么策略和执行系统必须分开

策略层只表达“想要什么仓位”，不直接表达“发什么订单”：

```text
strategy_id
symbol
target_quantity
decision_id
valid_until
metadata
```

执行平台根据经纪商真实持仓与未完成订单计算差额：

```text
delta = target - broker_position - signed_working_remaining
```

这样可以独立开发和替换信号研究，不把 IB 回调、partial fill、cancel race、重启恢复或风控逻辑复制到每个策略中。Gate A 判断策略是否值得交易；Gate B 判断执行平台是否足够安全。两张通行证彼此独立，任何一张通过都不推导另一张通过。

## 核心安全设计

### Durable-before-send

任何 broker write 之前必须先把 intent durable commit。数据库与 IB 之间不存在原子事务，因此提交结果不确定时进入 `SUBMISSION_UNCERTAIN + UNVERIFIED`，通过 reconciliation 收敛，绝不盲目重发。

### Broker 管事实，Journal 管含义

Broker 是持仓、订单和成交事实的权威；journal 是 decision、intent、HALT 原因和恢复语义的权威。恢复时比较 broker truth 与 journal-expected，而不是简单要求持仓归零。

### 四个正交状态维度

```text
link_state × sync_state × operating_mode × order_state[strategy, symbol]
```

Socket 已连接不代表账户状态可信。IB 1101 后即使连接恢复，订阅和账户快照仍需重新建立；只有完成稳定屏障的 reconciliation 才能恢复 `SYNCED`。

### HALT 必须跨重启保留

HALT 原因先落盘，重启时强制恢复。Operator acknowledgement 使用最新 durable HALT token 做 compare-and-set：旧界面不能误清除新事故，acknowledgement 也不会让当前进程自动恢复交易。

### Cancel-then-new

V1 不做原地改单：

```text
cancel → terminal callback → stable reconcile → new order
```

Cancel reject 不是终态；late fill、partial fill 和 callback 乱序都必须先回到 broker truth，再决定 replacement。

## 为什么先构建无 IB 核心

执行系统最危险的问题来自故障窗口，而不是正常下单路径，例如：

- WAL commit 后、`placeOrder` 前崩溃；
- `placeOrder` 返回前后无法判断请求是否到达 IB；
- cancel 与 fill 竞态；
- partial fill 后重启；
- Gateway 重启或 callback 延迟、乱序；
- durable HALT 落盘后进程被强制终止。

IB Paper 无法按测试要求稳定重现这些交错。因此状态机保持同步、可注入时钟，`FakeBroker` 提供可复现的故障序列，Hypothesis 和真实子进程强杀负责搜索边界情况。

这套方法已经发现并修复多类真实缺陷：cancel/fill 竞态导致盲目反向下单、重启后重复发送、reversal 被配置静默阻断、EOD flatten 不收敛、cancel reject 被误判终态、late execution 未更新内存、旧 HALT acknowledgement 清除新事故，以及非原子快照授权重复 replacement。

## 只读 SPY Recorder

Recorder 与交易路径隔离开发，只采集：

- SPY `BidAsk` tick-by-tick；
- SPY `AllLast` tick-by-tick；
- 5 秒 `TRADES` bars；
- IB connection/error、`marketDataType`、server time；
- local wall-clock 与 monotonic arrival timestamp。

原始事件通过有界内存队列交给独立 writer，按 batch 写入 append-only gzip JSONL；callback 不执行 gzip/flush/fsync。代码显式区分 `execution_minimal`、`evidence_sampled`、`research_full`，采样规则和 `handled → selected → enqueued → persisted → readback` 全链路均写入 manifest。独立 heartbeat publisher 不会替 event loop 刷新 pulse，因此 IB 请求整体卡住可以由外部 watchdog 发现。队列满、writer/heartbeat 异常、5 秒 bar heartbeat 丢失、关闭超时或任一计数不一致都会 fail-closed；BidAsk / AllLast 属于事件驱动流，其 staleness 只记录、不单独触发恢复。`execution_host` 不保存每条行情 tick，订单 Journal 的 durable-before-send 语义也没有被异步化。详细边界见 [Recorder 写入、测试与 Windows 部署边界](docs/RECORDER_STORAGE_AND_WINDOWS_POLICY_ZH.md)。

收盘 compact/finalize 同样有界：冻结一次 segment snapshot 后按 50,000 行写 Parquet row groups，健康统计使用 64 MB SQLite staging 保留乱序与精确 gap 语义，Parquet 按 batch 解码验证，所有候选验证完才以 manifest 作为最后完成标记。`FINALIZING` 使用独立 progress clock，不能伪装成 IB event-loop heartbeat。该修复针对 2026-08-14 Full-RTH 直接观察到的旧实现 16.23 GB private commit / 约 77 分钟收尾问题；同一 2,645,388 行 immutable raw 的 production-finalizer replay 已在 116.234 秒完成，峰值 working set 458,805,248 bytes、private commit 678,473,728 bytes、临时空间 191,734,866 bytes。下一次 Windows Full-RTH 仍需直接验证 capture→finalize 的端到端宿主与资源边界。

v4 只做不可变离线复核：原 v3 health/manifest 永不覆盖，输出是 create-only `health-v4.json` 与完成标记 `manifest-amendment-v4.json`。复核前必须匹配原 manifest 的完整 raw inventory/hash，解压结束后再次验证 segment identity/metadata/hash 未改变；raw schema 的缺字段、未知字段/type、非法 timestamp/number 都显式 FAIL。该离线双 hash verification 不改变阶段 C production finalizer 的一次 semantic decode + 一次 manifest hash scan。

当前实测已在 SPY OVERNIGHT 与正式 `RTH+SMART` bounded run 中直接观察到 LIVE BidAsk、AllLast 和 5 秒 bars 三路非零。早期 sleep 后读取 `Ticker.tickByTicks` 残余缓冲得到的 `AllLast=0` 已被判定为无效测量；现有 preflight 与 Recorder 都在 callback 中累计。2026-08-11 的 RTH handler-count run 中，handler 与 raw readback 均为 `8972/1707/25`，直接证明该窗口 callback→gzip→readback 无丢失。旧 preflight 与 Recorder 位于不重叠窗口，约 40% 的 BidAsk 计数差异从来不是有效的丢包测量，现已关闭且不再安排同步 A/B；这仍不等于 IB 上游无损或 Full-RTH 全日 health。

2026-08-12 又用真实 `QuoteRecorder.run()` 持有三路订阅执行一次经授权的 45 秒 outbound block：直接观察 1100→1102、connection epoch 不变、没有重订，三路均在 1102 后恢复。本轮没有 1101。旧进程还暴露一个审计问题：一个持续 outage 按 0.25 秒 poll 写了 380 条 `GAP_SUSPECTED`；新代码已改为 `FEED_OUTAGE / EXPECTED_SILENCE / GAP_SUSPECTED` 的 START/UPDATE/CHECKPOINT/END 生命周期，并要求 incident 后真实 BAR_5S 才能闭合，但该新逻辑尚未在真实 fault 上复测。详细边界见 [`docs/GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md`](docs/GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md)。

```powershell
# Broker-write-free Gateway 与稳定快照预检
.\.venv312\python.exe scripts\run_ib_readonly_preflight.py --port 4002

# 只有预检得到 market_data_type=1 且三路 sample 均非零后才运行
.\.venv312\python.exe -m ib_execution.quote_recorder `
  --root data\recordings --port 4002
```

上面的直接命令只适合有人看守的 bounded run。完整 RTH 不得从 Codex、IDE 或其他可能被关闭/升级的
交互式应用后台启动；2026-08-13 Recorder 曾因父 Codex AppX container 销毁而被连带强杀。Windows
Full-RTH 应使用 [`scripts/start_full_rth_recorder_task.py`](scripts/start_full_rth_recorder_task.py) 交给
Task Scheduler 独立托管，事故与修复边界见
[`docs/INCIDENT_FULL_RTH_20260813_APPX_TERMINATION_ZH.md`](docs/INCIDENT_FULL_RTH_20260813_APPX_TERMINATION_ZH.md)。
Task action 直接执行与 Recorder 相同的 Python PID；进程内 deadline 是真实 `RTH close + 3h30m`，Scheduler 另保留 `PT24H` 独立 backstop，覆盖 watchdog 启动前卡死。两层都不自动重启。

## 本项目明确不做什么

| 暂不实现 | 原因 |
|---|---|
| Watchdog 自动平仓或自动接管 | 存在 split-brain 风险。Watchdog 只告警并在验证进程身份后终止故障进程，不下单、不重启。 |
| MOC / auction orders | Cutoff 后不可撤回，且 Paper 无法充分验证。 |
| 原地改单 | Fill-vs-modify 竞态难以证明安全；V1 使用 cancel-then-new。 |
| 多策略共享账户 | 账户级持仓和人工订单归属会变得含糊；任何无法归属的 broker fact 都应 HALT。 |
| 复杂 reprice、多标的和微服务化 | 在 B1/B2 安全证据完成前只会增加状态空间。 |

Watchdog 不自动平仓的前提是 invariant 19：即使日终完全无法平仓并承受隔夜跳空，风险仍在预先批准的预算内。调整仓位上限时必须同时重审这一假设。

## 本地验证

```powershell
uv sync --locked --extra dev --extra ib
pytest -q
pytest -q -m property --hypothesis-profile=gate
python scripts\run_gate_b1.py
python scripts\demo.py
python scripts\deterministic_soak.py --seeds 150 --actions 100
python -m ib_execution.auditor data\journal.db
python -m ib_execution.provenance --check

# Gate B1.4 需要一个 64-128MB 的独立卷，fence 必须在另一个卷上
python scripts\run_storage_fault_drill.py --journal-volume X:\ --fence-dir C:\ProgramData\ibems
```

150 seeds × 100 actions 的 deterministic soak 已通过；300 × 150 没有在既定审查时限内完成，因此不宣称通过。

## 文档导航

- [系统规格](docs/SPEC.md)：状态、事件、接口和不变量定义。
- [最终执行计划](docs/FINAL_EXECUTION_PLAN_ZH.md)：Gate A/B/C/D、实施顺序和停止条件。
- [运行手册](docs/RUNBOOK.md)：环境、凭证、启动、告警和事故处理。
- [Recorder 写入、测试与 Windows 部署边界](docs/RECORDER_STORAGE_AND_WINDOWS_POLICY_ZH.md)：实际交易的数据边界、异步批量 writer、测试分层和 Windows order-capable 前置证据。
- [Gate B1 签字模板](docs/GATE_B1_SIGNOFF_TEMPLATE.md)：8 项 blocker、22 条不变量逐条签字、以及明确写下的范围边界。
- [v0.1.5 变更](docs/CHANGES_v0.1.5.md)：本版本安全修正。
- [v0.1.4 最终审查](docs/FINAL_REVIEW_V014_ZH.md)：HALT durability 等审查结论。
- `docs/adr/ADR-001` 至 `ADR-009`：关键架构决策。

凭证永远不得进入仓库、配置文件、日志或聊天记录。Gateway 负责认证，本平台不读取 IB 用户名或密码。

## 进程所有权与 fail-closed 边界

三条机制合起来，才让「停下来」这个决定能跨越线程、进程、重启和存储恢复。任何一条缺失都留一个缺口。

**不变量 0 —— 单写者进程所有权。** `Journal` 打开时对 sidecar 取 OS 级独占锁，在建表之前。SQLite WAL 允许多进程写，所以在此之前 single-writer 只是架构约定：两个 host 各自持有内存状态机、各自发单，而不变量 1–4 全部只在单进程内成立。锁由内核在进程死亡（含 SIGKILL）时释放，因此没有租约续期，也没有 stale lock 需要回收。被拒绝的进程**不接触数据库文件，也不连接 broker**。

**`execution_host` —— fail-closed 有了出口。** `fatal_shutdown_requested` 此前是核心置位、watchdog 从状态文件读取、而没有任何进程消费的一个 bool。现在 host 消费它并以退出码 10 结束；生产 supervisor 配置为 `Restart=no`（`deploy/`，由 `tests/test_supervisor.py` 解析断言）。启动顺序本身就是安全论证：日历 → fence 配置 → fence 状态 → journal 所有权 → journal restore，**全部在构造 broker 之前**。

**Durable fatal fence —— 22 号不变量够不到的那一段。** `_fail_closed_journal` 刻意不写 journal，因为 journal 正是失效的那个组件——这是对的，但后果不是：HALT 只存在于内存，进程退出，运维清出磁盘，下一个进程回放一个不含 HALT 的 journal，回到 `NORMAL` 继续交易。不变量 22 全程成立，只是从未被触及——它只能保护**已经落盘**的 HALT。fence 写在**另一个卷**上（journal 所在卷写满，正是最可能要写 fence 的原因；这条通过 `st_dev` 在启动时强制，不靠文档），退休是两阶段的：`RAISED` → 具名确认 → 只有对账解释了账户之后才 retire。确认本身不清除 fence，否则「点确认」就成了恢复交易的捷径。

```bash
python -m ib_execution.execution_host --journal D:\ibems-data\journal.db \
    --fence C:\ProgramData\ibems\fatal-fence.json --status D:\ibems-data\status.json
```

日历同理：`SUPPORTED_YEARS` 之外直接拒绝启动（退出码 13）。此前 `is_trading_day` 只问「是工作日且不在 2026 表里」，于是 2027 年每个 NYSE 假日都会被规划成 16:00 收盘的完整交易日——不报错，只是对着关闭的市场下单。没有任何不变量覆盖「假日表是否仍然有效」，所以再多生成式测试也抓不到。

## 下一步

1. **策略 Gate A 独立推进。** 在策略仓库完成真实成本、数据质量和统计不确定性判断；若结论为 `NO_GO` 或 `INSUFFICIENT_EVIDENCE`，且没有独立第二消费者，就停止投资交易型 IB Adapter。
2. **持三路订阅的受控断网已部分闭环。** production `run()` 已直接观察 1100→1102、不重订和恢复后逐流增量；1101 仍未观察，新 incident 生命周期尚未真实 fault 复测。不得为碰取 1101 重复断网。
3. **Windows lifecycle 证据已完成。** 2026-08-20 在目标主机对 exact commit `6d159f8` 执行 no-IB PASS/FAIL/HOLD Scheduler probe：Task Scheduler 直接拥有同一个 Python/Recorder PID，HOLD 的 launcher 退出后 task 继续存活，`/End` 后 PID 消失，只有允许的 direct-child Windows console host，没有意外 child/grandchild。清理后测试 task `0`、probe PID `0`、raw 文件 `0`；原 v3 verdict 不变。
4. **执行只读 B2 exact-tree freeze。** 官方 IB 文档逐项复核已于 2026-08-20 完成；新增的 executions window 官方歧义、非原子 snapshot 边界、cross-client 可见性限制与 completed-orders Read-Only 观测均已登记。F1 [B2 evidence schema v1](docs/GATE_B2_READ_ONLY_EVIDENCE_SCHEMA_V1_ZH.md) 已实现独立 validator/CLI；下一步按 [exact-tree freeze 实施计划](docs/GATE_B2_EXACT_TREE_FREEZE_PLAN_20260820_ZH.md) 执行 F2，从 Git objects 和受控 evidence roots 构建、复核 candidate manifest，再进入 F3 metadata-only freeze。D1/D2 assumption review 按 owner 决定不阻塞本次只读 freeze，但任何 order-capable Paper/Live 或生产部署前仍必须完成。
5. **只读阶段不下单。** `completed orders` 被 Gateway Read-Only policy 阻断；不关闭保护追测。非空 reconciliation、订单身份和 callback 保留到另行授权的 paper-order 子阶段。
6. **paper order 必须重新授权。** 只读证据封存后，owner 才单独决定是否运行 1 股 SPY paper-order protocol；B1 PASS 或 B2 只读结果都不自动构成该授权。MOC、多策略、live capital 和自动 watchdog takeover 继续推迟；live order 继续禁止。

当前第二个独立使用者仍为 `NONE_CONFIRMED`。QQQ 与 SPY 属于同一日内动量命题，不能单独证明继续建设交易 Adapter 的经济必要性。**平台越成熟，越不能反过来成为「所以策略应该交易」的理由。**
