# Full-RTH 只读 Recorder 提前终止事故报告（2026-08-12）

- 状态：**OPEN / Full-RTH 未通过 / 修复尚未实施**
- 事故窗口：`2026-08-12 21:28:47–22:36:44 HKT`（`13:28:47–14:36:44 UTC`）
- 运行标识：`20260812_full_rth_main_83e9573_v3`，Recorder run id `0313672deb`，API client id `964`
- 安全边界：IB Gateway paper port `4002`、`readonly=True`、SPY `RTH+SMART`、`research_full`；没有发送、修改或取消订单，没有人为故障注入，`order_authorization=NONE` 不变。

## 1. 结论摘要

这次运行不是 Full-RTH PASS。Recorder 从开盘开始连续取得 LIVE BidAsk、AllLast 和 5 秒 bar，但在约 66 分钟有效覆盖后，三路采集订阅停止产生新事件。Liveness 在 bar age `12.3s`、最后任一采集事件 age `10.0s` 时触发恢复；durable event 明确记录 `evidence_of_life=True`，代码却选择了 `full_reconnect`。重连后的新行情请求收到 IB error `10197`，Recorder 按预期 fail-closed，最终 health 为 FAIL，RTH 覆盖约 `17%`。

事故没有产生资本或持仓风险：本轮完全只读，稳定快照为零持仓、零 open orders、零 executions。写盘链本身完整：`1,261,833` 行 accepted/enqueued/persisted/readback 一致，`dropped_count=0`、`writer_error=null`，14 个 gzip segment 均完整。

可以确认的代码缺陷是：`RecoveryScheduler.plan()` 在检查 `evidence_of_life` 之前先消耗默认的两次 fast full-reconnect，因此实现违反了同一文件中“有生命迹象时只修复最小故障”的设计说明。现有测试把 `fast_attempts` 人为设为 `0`，没有覆盖生产默认值，因而没有发现此缺陷。

`10197` 的服务器会话归因仍未闭环。Owner 于 2026-08-13 明确确认：**其本人没有主动登录真实账户；如果 IB 侧存在另一个真实账户会话，并非 owner 主动建立。** 这项陈述应作为 operator testimony 保留；它不能单独证明 IB 服务器当时不存在残留、他端、Client Portal、移动端或误分类会话。当前证据也不能证明凭证被他人使用。

2026-08-13 00:41 HKT 的独立只读 probe 再次取得 `marketDataType=1 (LIVE)`，120 秒内收到 `8,786 / 1,614 / 25` 条 BidAsk / AllLast / 5 秒 bars，且没有 `10197`。因此持久 entitlement、route 或订阅参数错误已基本排除；事故更符合瞬时订阅/会话所有权或重连时序问题。

## 2. 影响与判定

| 项目 | 判定 |
|---|---|
| Full-RTH 全日 health | **FAIL / 未完成**；不能用前 66 分钟非零数据升级为 PASS |
| 市场数据 | 约 66 分钟有效，三路覆盖约 `16.99%–17.00%`；其余时段缺失 |
| 写入完整性 | 已验证；无 Recorder drop、无 writer error、无不完整 gzip segment |
| 订单、持仓、资本 | 无影响；只读运行，无 broker write |
| Gate B2 | 仍为 `READ_ONLY_IN_PROGRESS` |
| Paper/Live order 授权 | 仍为 `NONE` |
| 后续 Full-RTH | 修复和短时真实 smoke 前不得重跑并宣称可采证据 |

## 3. 时间线

| UTC | HKT | 直接观测 |
|---|---|---|
| 13:28:47 | 21:28:47 | client 964 启动并连接 Gateway，READ_ONLY |
| 13:30:05 | 21:30:05 | 首批 RTH LIVE 数据进入 Recorder |
| 14:36:21.456 | 22:36:21.456 | 最后一条持久化 BAR_5S；API plaintext 对应 request 6 的最后 real-time bar |
| 14:36:23.705–.709 | 22:36:23.705–.709 | 最后 AllLast / BidAsk；API plaintext 对应 tick-by-tick requests 5 / 4，与 Recorder 尾部一致 |
| 14:36:26.798 | 22:36:26.798 | API plaintext 中普通 L1 request 3 仍有 price/size/string 更新；说明 Gateway→client 通道并非与三路采集订阅同时停止 |
| 14:36:33.718 | 22:36:33.718 | `GAP_SUSPECTED_START`：bar age `12.3s`、BidAsk age `10.0s` |
| 14:36:33.718 | 22:36:33.718 | `RECOVERY_ATTEMPT:plan=full_reconnect;evidence_of_life=True`，随后 intentional close |
| 14:36:39.576 local receive | 22:36:39.576 | connection epoch 2 建立，仍为 READ_ONLY |
| 14:36:44.602 | 22:36:44.602 | Gateway callback 返回 `IB_ERROR:10197:reqId=3` |
| 14:36:44.704 | 22:36:44.704 | Recorder prerequisite fail-closed，intentional close，finalize health/manifest |
| 16:41:15–16:43:28 | 次日 00:41–00:43 | client 965 只读 probe PASS，三路 LIVE，未见 `10197` |

Windows System event log 在事故前后 `22:25–22:45 HKT` 没有 sleep、wake、shutdown、restart 或 network-profile 事件。最近一次 sleep/wake 是 2026-08-10 23:43 HKT。Gateway 进程 PID 1960 从 2026-08-12 20:00:15 HKT 持续运行，事故后仍监听 port 4002。因此“电脑睡眠或 Gateway 进程重启”在本轮证据下已排除。

## 4. 直接证据

### 4.1 Recorder artifact（本地保留，不提交大体积行情）

根目录：`artifacts/ib_preflight/20260812_full_rth_main_83e9573_v3/`

| 文件 | SHA-256 |
|---|---|
| `raw/2026-08-12/health.json` | `4AC241BEBC39F5CABC8DEE16024F3CE761EA32B1708F86D0B5E29FDF6A7146C9` |
| `raw/2026-08-12/manifest.json` | `0068D7E3723E577E76C0E5408FEF14D7F14AC3763B162243F9F0F0726D465DC6` |

主要 accounting：

- rows / accepted / enqueued / persisted / readback：均为 `1,261,833`；
- required handler/selected：`1,261,756`，另有 `77` 条 SYSTEM；
- BidAsk / AllLast / BAR_5S：`1,135,171 / 125,789 / 796`；
- queue high-water：`433 / 100,000`；
- dropped：`0`；writer error：`null`；
- gzip：14/14 完整，无 salvage、无 trailing partial bytes；
- health：`ok=false`，fatal error `10197`，一个未闭合 `GAP_SUSPECTED`。

### 4.2 Gateway API logs

加密原始文件仍在 Gateway log 目录，不进入 Git：

| 文件 | 大小 | SHA-256 | 当前可读性 |
|---|---:|---|---|
| `api.964.20260812.212847.ibgzenc` | 156,888,427 bytes | `04E93FCFB1E4E71B70CE5D66C5E89B6218EDD55823E4B36249109E1B5EA752F6` | 已包含在 operator 导出的 plaintext 中，覆盖重连前 |
| `api.964.20260812.223633.ibgzenc` | 4,255 bytes | `ADABA2CEB2DDCD5E6CC3B83D451E225E9A27FAF1D4422CC937149914B87B28BF` | **尚未包含在 plaintext export**；它应覆盖重连后至 `10197` 的最后约 11 秒 |

2026-08-13 重新导出的 `api-exported-logs.txt` 为 `216,361,685` bytes，SHA-256 `8AD06A55612FE10E6B780B2304EB5293F4034BA7B81E78AEEC1AB4FA1ECA3EF1`。它包含 client 964 的第一个大文件，但没有第二个 4KB 文件。该 plaintext 含账户标识及其他敏感 API 内容，**不得提交仓库或公开分享**。

从已导出的 client 964 部分直接得到：

- 本轮只出现 `2104 / 2107 / 2158 / 2119 / 2106` 等 farm 状态信息，没有 1100/1101/1102 或 fatal error；
- tick-by-tick BidAsk request 4 最后一条为 `22:36:23.706 HKT`；
- tick-by-tick AllLast request 5 最后一条为 `22:36:23.705 HKT`；
- real-time bar request 6 最后一条为 `22:36:21.455 HKT`；
- 普通 L1 request 3 继续收到消息至 `22:36:26.798 HKT`；
- 随后第一个 API log 结束，最终 `10197` 所在的第二个小文件还未被解密导出。

### 4.3 事故后 LIVE probe

- 文件：`artifacts/ib_preflight/20260813_current_live_diagnosis_v1/report.json`
- SHA-256：`F13956A327971319F0C8F200C1E84E2BEA0DC0BB82E3F422A22FCD7E5E89CC9B`

该轮 `readonly=true`、`marketDataType=1`、三路非零、120.187 秒窗口完整、`no_fatal_entitlement_error=true`，最终 `passed=true`。它只证明事故后的短窗口正常，不是 Full-RTH，也不反证事故窗口内曾出现瞬时服务器会话冲突。

## 5. 技术分析

### 5.1 已确认：恢复计划与设计说明相矛盾

[`RecoveryScheduler`](../src/ib_execution/quote_recorder.py#L1332) 的说明要求“bar 停止而 quotes 仍活跃时，只重订 bar；全部没有生命迹象时才 full reconnect”。但 [`plan()`](../src/ib_execution/quote_recorder.py#L1408) 当前顺序是：

1. 先检查 `fast_used < fast_attempts`，默认允许 2 次；
2. 直接返回 `FULL_RECONNECT`；
3. 之后才检查 `evidence_of_life`。

事故中 [`_recover_market_data()`](../src/ib_execution/quote_recorder.py#L1965) 计算出 `evidence_of_life=True`，durable event 也保存了这一值，仍被上述分支送入 full reconnect。这是确定性代码行为，不依赖对 IB 的猜测。

### 5.2 已确认：测试没有覆盖生产默认组合

[`test_live_quotes_veto_a_reconnect_and_get_a_targeted_bar_repair`](../tests/test_market_liveness.py#L572) 把 scheduler 构造为 `fast_attempts=0`，因此绕过了生产默认分支。另一个测试只验证 `evidence_of_life=False` 时默认两次 fast reconnect。缺少的回归组合是：**默认 `fast_attempts=2` + `evidence_of_life=True` 必须拒绝 full reconnect**。

### 5.3 已确认：这不是整个 socket 立即断开

三路正式采集订阅在 22:36:21–23 停止，但 L1 request 3 继续到 22:36:26，期间没有 Gateway error。它证明 Gateway→client 仍传输了一类市场消息，不能把事故简单归为电脑断网或 Gateway crash。

目前尚不能区分：

- IB 侧 tick-by-tick + real-time-bars 短暂停发；
- Gateway 内部某组订阅/队列短暂停滞；
- client callback/event-loop 对不同消息类型的处理差异；
- 请求/记录时间戳之间的短暂排队。

因此报告不把“IB 上游断流”或“客户端 event loop stall”中的任何一个写成根因。修复前应增加按 request/stream 记录最后 protocol evidence 的诊断，并基于真实 cadence 决定是 targeted bar repair、targeted all-stream resubscribe，还是最后才 full reconnect。

### 5.4 部分确认：`10197` 是 fatal callback，竞争会话的主体未证实

Recorder durable row 保存了 `IB_ERROR:10197:reqId=3`，随后 prerequisite checker 正确将整轮判为失败。IB 官方文档将 10197 定义为 competing live session 下没有 market data，并说明 paper 用户共享 live 用户的市场数据时，live 用户在 TWS/IB Gateway 或 Client Portal 等其他位置登录会影响 paper 数据：

- [TWS API message codes](https://interactivebrokers.github.io/tws-api/message_codes.html)
- [TWS API market data](https://interactivebrokers.github.io/tws-api/market_data.html)
- [TWS API initial setup](https://interactivebrokers.github.io/tws-api/initial_setup.html)

但本地日志不能查询 IB 服务器的真实会话所有权，也不能识别谁、从哪里登录。Owner 已明确否认主动登录；仍可能是 stale server session、其他终端/Client Portal、他人使用账户、或 IB 在快速重订阅时的瞬时误分类。当前证据不能在这些可能性之间排序，也不能指控存在未经授权登录。

### 5.5 已确认：当前订阅参数和 entitlement 可以工作

事故后 client 965 使用同一 Gateway、SPY `RTH+SMART` 和同样三路数据策略取得 LIVE 数据并通过完整 120 秒 sample window。持久 entitlement 缺失、错误 exchange、unsupported generic tick 49、以及“该策略永远取不到 live data”均不符合当前直接观测。

### 5.6 次要报告问题

`health.json` 同时写出“一个 incident totalling 0s”和“open for 11s”。原因是 completed-total 不包含 open incident，而后者在另一字段单独计算。机器字段仍能恢复事实，但读者文本具有误导性；应使 open duration 明确进入问题摘要，或把第一句限定为 completed incidents。

此外，artifact 目录名包含 `main_83e9573`，但 health/manifest 本身没有加密绑定 Git commit、配置树和依赖锁。事故报告可记录启动 commit，后续正式 evidence bundle 仍应补齐 self-contained provenance，不能仅依赖目录命名。

## 6. 根因分层

| 层级 | 当前结论 | 置信度 |
|---|---|---|
| 触发条件 | BAR_5S、BidAsk、AllLast 在约 2 秒内停止；L1 短暂继续 | 高，API plaintext + Recorder 一致 |
| 放大因素 | scheduler 在 `evidence_of_life=True` 时错误 full reconnect | 高，可由源码和 durable event 确定 |
| 最终终止 | 重连后 reqId 3 收到 fatal `10197`，Recorder fail-closed | 高，Recorder durable evidence；API 第二小文件待导出 |
| `10197` 会话主体 | 未知；owner 明确没有主动登录 | 未证实 |
| 原始三路静默归属 | IB、Gateway 或 client 内部路径尚不能区分 | 未证实 |
| 电脑休眠 / Gateway crash | 与系统和进程证据不符 | 已排除 |
| 持久 entitlement / route / 参数错误 | 与事故后同策略 LIVE PASS 不符 | 基本排除 |

不作如下反事实声明：如果没有 full reconnect，数据“一定会自行恢复”。现有证据只能证明 full reconnect 违反了既定最小修复策略，并使 Recorder 进入了随后收到 `10197` 的请求路径；不能证明它是 10197 的服务器根因。

## 7. 整改与重新验证条件

### P0：再次 Full-RTH 前必须完成

1. 调整 `RecoveryScheduler.plan()` 的决策顺序：`evidence_of_life=True` 必须先否决 destructive full reconnect；保留全静默时的有界 fast reconnect。
2. 增加生产默认值回归：默认 `fast_attempts=2` + live evidence 首次只能 `BARS_ONLY`（或经批准的新 targeted plan），不得 `FULL_RECONNECT`。
3. 复核“只有 L1 活跃、三路采集订阅均 stale”这一新观测，决定是否需要 targeted all-stream resubscribe；在决定前不要把单纯 bars-only 写成已证明充分。
4. 运行 narrow liveness/resilience tests、affected regression 和 `python -m ib_execution.provenance --check`。
5. 用当前修复代码执行一次 10–20 分钟只读 smoke；确认 LIVE、三路持续、无意外 full reconnect。该 smoke 不是 Full-RTH PASS。

### P1：证据闭环

1. 只导出 `api.964.20260812.223633.ibgzenc` 这个 4KB 文件；无需导出 666MB Gateway 总日志。
2. 核对 plaintext 中 reconnect 后的 `reqMktData` 和原始 `10197` 行；记录协议方向、reqId 和时间，不提交账户标识。
3. 若 `10197` 再次出现，在不增加登录或订单风险的前提下联系 IB support，提供 UTC 时间、client id、reqId、错误码及经脱敏的日志哈希，请其检查服务器 session ownership。

### P2：报告与 provenance

1. 修正 open incident 的“0s total”读者文本。
2. 在正式 evidence manifest 中绑定 Git commit、source/config/lock hashes 和运行参数。

受控断网仍是独立测试，不能与本事故的正常行情静默混为一谈。任何新 network fault injection 仍需 owner 对精确动作、目标和窗口重新授权；不得为了碰取 1101 反复断网。

## 8. 当前状态

- `verified`：Full-RTH 失败；三路尾部时间；写盘完整；scheduler 决策缺陷；`10197` durable callback；事故后 LIVE probe；无 sleep/Gateway restart。
- `partially verified`：10197 符合 IB competing-session 语义，但最终 4KB API plaintext 尚缺。
- `not verified`：谁或什么持有 competing session；三路最初静默位于 IB、Gateway 还是 client 的具体边界；无 full reconnect 时是否会自行恢复。
- corrective code：**未修改**。
- Full-RTH：**未通过，待修复后另日重跑**。

## 8. Amendment：P0 recovery 修复实现（2026-08-13）

后续代码复核确认事故报告 §5.1 之外还有两条独立生产缺陷：

1. 主循环把任何 `LivenessAction.CONTINUE` 都当成恢复并调用 `note_recovered()`。重连后的 12 秒 grace
   period 即使一条新 `BAR_5S` 都没收到也会返回 `CONTINUE`，因此 `fast_used` 会被反复清零，持续故障下
   slow backoff 可能永远无法启动。修复后只有真实 `BAR_5S` handler 才能重置 recovery state。
2. `1101` 与 `10225` 原先通过 `_resubscribe -> ConnectionError` 的平行 shortcut 无条件做 socket 级重连，
   完全绕过 `RecoveryScheduler`。修复后删除该 shortcut：`1101 -> ALL_MARKET_STREAMS`、
   `10225 -> BARS_ONLY`、`1102 -> NONE`，统一进入一条 recovery pipeline。

同时增加 veto-only `transport_evidence`：普通 L1/其他 inbound activity 只能证明当前 transport 尚活，不能因
自身沉默触发任何动作；当三路 capture 都 stale 但 transport 尚有证据时，新增 `ALL_MARKET_STREAMS` 仅取消并
重建 BidAsk / AllLast / BAR_5S 三路订阅，不动 socket 或 L1 probe。只有 capture 与 transport evidence 都缺失
时才允许 full reconnect。`10197` 继续保持 fatal prerequisite，不改成自动无限重试。

本 amendment 只说明 P0 代码与离线回归的整改范围；**不改变本次事故 FAIL，不构成 Full-RTH PASS，也不替代
修复后的 10–20 分钟真实只读 smoke。**
