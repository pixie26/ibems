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
| 只读 SPY Recorder / B2 preflight | Gateway 4002、server time、SPY `conId=756733`、account summary、空状态 broker snapshot、多 client、Gateway restart / `TerminateProcess` 与 `1100 -> 1102` 已有直接观测。明确 `OVERNIGHT` 和正式 `RTH+SMART` 的三路行情及 bounded Recorder 写盘均已 PASS；仍不是 Full-RTH 全日 health。 |
| 交易型 IB Adapter | 未授权连接下单路径。`placeOrder`、`cancelOrder`、订单身份、完整 callback/error mapping 和非空动态 reconciliation 尚未在真实 Gateway 验证。 |
| Emergency flatten broker path | 未实现。现有代码只覆盖计划生成与人工确认边界。 |

详细状态与可复查证据：

- **[`STATE.json`](STATE.json) —— 唯一权威的机器可读状态**（gate 状态、source/config/lock 三个树 hash）
- [Gate B2 当前状态摘要](docs/GATE_B2_STATUS_20260810_ZH.md)（当前真实 Gateway 只读测试状态、证据索引和下一步）
- [Gate B2 只读详细证据](docs/GATE_B2_READONLY_20260809.md)
- [Gate B2 SPY OVERNIGHT 行情与 Recorder 证据](docs/GATE_B2_OVERNIGHT_20260810.md)
- [Gate B2 SPY RTH 行情与 Recorder 证据](docs/GATE_B2_RTH_20260810.md)
- [IB documented-vs-observed 矩阵](docs/DOCUMENTED_VS_OBSERVED.md)
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

原始事件以 append-only gzip JSONL 滚动保存，收盘后原子生成 Parquet、健康报告和 SHA-256 manifest。健康报告检查 LIVE 数据、三路覆盖、最大 gap、断线、时钟偏差、行数和文件 hash。

当前实测连接正常。2026-08-07 在 4002（paper 账户已 redact）的预检确认：IB `10089` entitlement 阻塞**已解除**，`marketDataType=1`（Live），`entitlement_blocked=false`。这一条是直接观测，成立。

同一次预检报出的 `AllLast=0` **是无效证据，不能用来推断任何事。** 当时 `run_ib_readonly_preflight.py` 在 sleep 结束后读取 `Ticker.tickByTicks` 的**残余内容**，而 `ib_async` 会在每次网络更新之间清空该缓冲区——所以那个 0 只说明「最后一次 flush 里没有 AllLast」，不说明「20 秒内 IB 没有推送 AllLast」。同一次预检里 `bars_5s=4` 之所以正确，仅仅因为它读的是会累积的 `RealTimeBarList`。

已知的只有一件事：**entitlement 不是原因**（已直接观测到 LIVE）。`AllLast` 订阅路径本身是否正常，要等改成 event-driven 累积计数之后重测才能判断——`reqRealTimeBars` 正常并不能推出 `reqTickByTickData` 正常，两者是不同的请求路径。

在预检得到 `market_data_type=1` 且三路 sample 均非零之前，Recorder 仍会 fail-closed、退出码 2。

```powershell
# Broker-write-free Gateway 与稳定快照预检
.\.venv312\Scripts\python.exe scripts\run_ib_readonly_preflight.py --port 4002

# 只有预检得到 market_data_type=1 且三路 sample 均非零后才运行
.\.venv312\Scripts\python.exe -m ib_execution.quote_recorder `
  --root data\recordings --port 4002
```

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
2. **Gate B2 RTH 行情证据已完成。** 正式 `RTH+SMART` preflight 与 bounded Recorder 均运行超过 120 秒，LIVE BidAsk / AllLast / 5s bars 全部非零；该结果仍不等于 Full-RTH 全日 coverage。
3. **完成 documented-vs-observed 复核与 B2 freeze。** 周末空状态、client/Gateway 故障和 `1100 -> 1102` 已有直接观测；仍需官方文档逐项复核、Windows/provenance gap 处置，并把 B2 source、tests、docs 和 evidence 绑定到新的可复查 tree。
4. **只读阶段不下单。** `completed orders` 被 Gateway Read-Only policy 阻断；不关闭保护追测。非空 reconciliation、订单身份和 callback 保留到另行授权的 paper-order 子阶段。
5. **paper order 必须重新授权。** 只读证据封存后，owner 才单独决定是否运行 1 股 SPY paper-order protocol；B1 PASS 或 B2 只读结果都不自动构成该授权。MOC、多策略、live capital 和自动 watchdog takeover 继续推迟。

当前第二个独立使用者仍为 `NONE_CONFIRMED`。QQQ 与 SPY 属于同一日内动量命题，不能单独证明继续建设交易 Adapter 的经济必要性。**平台越成熟，越不能反过来成为「所以策略应该交易」的理由。**
