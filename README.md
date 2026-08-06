# IB 执行与风控平台

`ib-execution-platform` 是一个面向单账户、单正常写入者的 IBKR 策略执行与风控平台。策略只提交目标仓位，平台负责风险检查、订单生命周期、持久化、重启恢复和经纪商状态对账。

```text
策略 / 信号 → TargetPosition → 风险引擎 → OMS → IBKR
```

> **安全状态：禁止把交易 Adapter 连接到 IB Paper 或 Live。**
>
> 当前版本是 `v0.1.5.dev0 / Phase 0 reviewed baseline / Specification frozen`，但 **Gate B1 尚未通过**。只读 Recorder 可以连接 Gateway 做预检；在实时行情权限和整日数据健康报告通过前，不得视为可上线。

核心原则只有一句：**安全优先于可用性；无法证明状态可信时就停止。**

## 当前状态

| 模块 | 状态 |
|---|---|
| 无 IB 依赖的执行核心 | 已审查原型。Python 3.12.13 下当前工作树 149 项测试全部通过，其中 144 项 non-property、5 项 property；包含 7 个子进程强杀窗口和 6 类 journal/queue fail-closed 场景。 |
| Hypothesis Gate campaign | 已提交的 Phase 0 基线中，两项生成式测试各通过 1,500 examples，seed 为 `2026080601`。2026-08-07 的 Recorder-only 修改已跑完整默认回归；下一次 B1 正式签字前应重新运行 formal campaign。 |
| 22 条安全不变量 | Property / Runtime / Auditor 三重证据入口已齐；真实宿主退出、OS/卷级故障演练和正式评审签字仍未完成。 |
| 只读 SPY Recorder | 4002 Gateway 握手、server time、SPY 合约解析和静态三轮账户快照读取成功。IB 返回 `10089`：缺少 `SPY ARCA/TOP/ALL` API 实时行情权限，因此尚无合格 Full-RTH session。 |
| 交易型 IB Adapter | 未实现、未连接。`placeOrder`、`cancelOrder`、完整 callback/error mapping 和动态 stable-snapshot protocol 均属于 Gate B2。 |
| Emergency flatten broker path | 未实现。现有代码只覆盖计划生成与人工确认边界。 |

详细状态与可复查证据：

- [实施状态](docs/IMPLEMENTATION_STATUS.md)
- [验证清单](VALIDATION_MANIFEST.txt)
- [22 条不变量覆盖矩阵](docs/INVARIANT_COVERAGE.md)
- [审查与执行结论](docs/REVIEW_AND_EXECUTION_20260806_ZH.md)

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

当前实测连接正常，但行情预检被 IB `10089` 阻止。Recorder 会在约 2 秒内 fail-closed、退出码 2，并保留 unhealthy 证据包，不会对不可重试的 entitlement 错误反复重连。

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
pip install -e ".[dev]"
pytest -q
pytest -q -m property --hypothesis-profile=gate
python scripts\run_gate_b1.py
python scripts\demo.py
python scripts\deterministic_soak.py --seeds 150 --actions 100
python -m ib_execution.auditor data\journal.db
```

150 seeds × 100 actions 的 deterministic soak 已通过；300 × 150 没有在既定审查时限内完成，因此不宣称通过。

## 文档导航

- [系统规格](docs/SPEC.md)：状态、事件、接口和不变量定义。
- [最终执行计划](docs/FINAL_EXECUTION_PLAN_ZH.md)：Gate A/B/C/D、实施顺序和停止条件。
- [运行手册](docs/RUNBOOK.md)：环境、凭证、启动、告警和事故处理。
- [v0.1.5 变更](docs/CHANGES_v0.1.5.md)：本版本安全修正。
- [v0.1.4 最终审查](docs/FINAL_REVIEW_V014_ZH.md)：HALT durability 等审查结论。
- `docs/adr/ADR-001` 至 `ADR-009`：关键架构决策。

凭证永远不得进入仓库、配置文件、日志或聊天记录。Gateway 负责认证，本平台不读取 IB 用户名或密码。

## 下一步

1. **策略 Gate A 独立推进。** 在策略仓库完成真实成本、数据质量和统计不确定性判断；若结论为 `NO_GO` 或 `INSUFFICIENT_EVIDENCE`，且没有独立第二消费者，就停止投资交易型 IB Adapter。
2. **补齐 Recorder 权限并开始不可追回的数据采集。** 开通覆盖 SPY/NYSE Arca 的 API LIVE 行情，预检三路 sample 后，从下一个完整 RTH 开始采集和每日健康审计。
3. **完成 Gate B1。** 补真实 execution-engine 宿主退出/监督器、OS 或受限卷级 disk-full/WAL 演练，以及 22 条不变量正式评审签字。
4. **B1 签字后才进入 Gate B2。** 先做只连接、只读账户事实和动态 stable-snapshot protocol，再做人工 1 股 paper target/cancel；MOC、多策略和自动 watchdog takeover 继续推迟。

当前第二个独立使用者仍为 `NONE_CONFIRMED`。QQQ 与 SPY 属于同一日内动量命题，不能单独证明继续建设交易 Adapter 的经济必要性。
