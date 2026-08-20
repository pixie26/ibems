# Gate B2：IB Gateway documented-vs-observed 矩阵

状态：**OFFICIAL REVIEW COMPLETE；OBSERVATION/FREEZE IN PROGRESS**

官方页面复核日期：`2026-08-20`

范围：IB Gateway paper account；当前仅只读。订单相关项目在 owner 明确授权 paper-order 子阶段前保持 `NOT TESTED`。

当前状态、证据索引和下一步见 [`GATE_B2_STATUS_20260810_ZH.md`](GATE_B2_STATUS_20260810_ZH.md)；只读 exact-tree freeze 的范围与执行步骤见 [`GATE_B2_EXACT_TREE_FREEZE_PLAN_20260820_ZH.md`](GATE_B2_EXACT_TREE_FREEZE_PLAN_20260820_ZH.md)。

## 1. 判定方法

- `DOCUMENTED`：当前 IBKR Campus 页面明确给出的接口或错误码语义；不是运行时证明。
- `OBSERVED`：本项目在真实 paper Gateway 上直接观察到；只覆盖记录的版本、配置、账户状态和窗口。
- `OFFICIAL DOC AMBIGUITY`：IBKR 当前官方页面内部存在冲突，项目不得自行选择更有利解释。
- `DESIGN / NOT GUARANTEED`：属于本项目的安全设计；官方页面没有承诺该性质。
- `OUT OF IB DOC SCOPE`：Windows/进程等行为，不应伪装成 IB API 官方语义。

官方文档、单元测试和历史上下文都不能替代直接观测；直接观测也不能外推到未覆盖状态。找不到官方保证时，结论是“未保证”，不是 `PENDING`，更不能用记忆补齐。

## 2. 复核发现的关键语义缺口

1. `accountSummaryEnd`、`positionEnd`、`openOrderEnd`、`execDetailsEnd` 和 `contractDetailsEnd` 只分别表示对应请求/数据流结束。官方页面没有承诺这些结束点之间形成原子、同一时刻或相互一致的 broker snapshot。
2. executions 回看窗口存在官方冲突：请求页写“当日午夜后”，接收页写“最近 24 小时”。在 IBKR 澄清或以更强 broker authority 补齐前，reconciliation 必须把边界交易视为可能缺失，不能以任一说法恢复 `SYNCED`。
3. `reqAllOpenOrders` 是一次性请求，不是持续订阅；可见性还受 API username、client ID 和 Master/client-0 配置影响。空状态 `0/0/0` 不能证明跨 client 的非空完整性。
4. 当前 completed-orders 页面说明请求与 callback，但没有为 Read-Only 模式给出兼容保证，接收页也未说明可依赖的 completion callback。Gateway 将该请求拦为“需要 API 写权限”属于本轮真实观测，不能改写成普遍官方规则。
5. `reqCurrentTime` 官方页面说明返回 broker timestamp；没有给出旧异步调用的最小请求间隔或 callback deadline。项目采用的 1.1 秒 request-before pacing 是实测后的客户端加固，不是 IB 保证。

## 3. 逐项矩阵

| 项目 | 官方文档复核结论 | 真实 Gateway 观测 | 当前结论 / 边界 |
|---|---|---|---|
| API TCP handshake | 初始 TCP socket 后进行版本 handshake；连接完成时会取得 account、`nextValidId`、connection time 等初始消息。`nextValidId` 常用作连接完成信号；过早请求可能被丢弃 | Windows paper port 4002、`readonly=True` 成功；server version 178 | `DOCUMENTED AND OBSERVED`；socket connected 本身不等于 ready/synced |
| Managed accounts | `managedAccounts` 在初始连接时自动返回该 API username 可用账户 | 返回 1 个账户；报告只保存 count | `DOCUMENTED AND OBSERVED`；数量不证明账户授权范围正确，且不落盘 account id |
| SPY contract qualification | `reqContractDetails` 返回所有匹配合约，`contractDetailsEnd` 表示该请求结束 | `SMART/ARCA/USD` 唯一解析，`conId=756733`，1 条 details | `DOCUMENTED AND OBSERVED`；唯一性由本次响应证明，不跨时间外推 |
| Market-data type / entitlement | `marketDataType=1` 是 regular/live；实时 bars 需要相应 L1 subscription | OVERNIGHT 与 RTH 均见 type 1；RTH v1 另见 competing-session 10197 | `DOCUMENTED AND OBSERVED`；10197 后即使已有 ticks 也 fail closed |
| Overnight routing | 官方 overnight 课程要求 eligible US stocks/ETFs 使用 `OVERNIGHT` destination；与普通 SMART route 分开 | `OVERNIGHT+SMART` 为 `0/0/0`；明确 `OVERNIGHT+OVERNIGHT` 后取得三路数据 | `DOCUMENTED AND OBSERVED`；route/label 不匹配在连接前拒绝 |
| BidAsk / AllLast / 5-second bars | tick-by-tick 类型包括 `Last`、`AllLast`、`BidAsk`、`MidPoint`；realtime bars 固定为 5 秒并有 subscription/pacing 约束 | OVERNIGHT 120 秒 `1620/13/25`；RTH handler/readback 均 `8972/1707/25`；Full-RTH v4 health PASS | `DOCUMENTED AND OBSERVED`；证明本地窗口接收/落盘，不证明交易所到 IB 端到端完整性 |
| Broker clock | `reqCurrentTime` 返回当前 broker timestamp | 7 个 RTT midpoint 样本中位偏差 `+0.517s`，最大绝对值 `0.837s` | `DOCUMENTED AND OBSERVED`；只覆盖该窗口 |
| Repeated `reqCurrentTime` | 官方未给出旧异步调用的最小间隔或 callback deadline | 0.2 秒间隔出现 callback 未返回；1.1 秒 request-before pacing 后完成 | `OBSERVED - CLIENT HARDENED`；不得称 1.1 秒为官方 pacing |
| Account summary | `accountSummaryEnd` 表示该次 account-summary 信息已返回完毕 | 0.141 秒完成，71 项；只保存 count/hash | `DOCUMENTED AND OBSERVED`；单请求完成不等于全 broker 原子快照 |
| Positions | initial positions stream 后以 `positionEnd` 表示传输结束 | 三轮 `0/0/0` 中 positions 均为 0，hash 相同 | `STATIC CANDIDATE ONLY`；未覆盖持仓变化并发 |
| All open orders | `reqAllOpenOrders` 返回 associated accounts 的当前 open orders；一次性、非订阅。`reqOpenOrders` 仅同 client，client 0 绑定还会改变 order ID 且 Read-Only 下会被拒绝 | 三轮 open orders 均为 0，hash 相同 | `STATIC CANDIDATE ONLY`；未证明其他 client/username 的非空可见性 |
| Executions | `execDetailsEnd` 表示该请求结束；官方请求页称“当日午夜后”，接收页称“最近 24 小时” | 三轮 executions 均为 0，hash 相同 | `OFFICIAL DOC AMBIGUITY / STATIC ONLY`；边界成交必须视为可能缺失 |
| 双快照稳定屏障 | 官方没有保证 positions/open orders/executions 的结束点构成原子或互相一致 snapshot | 连续 pair 的整体/分项 hash 相同；empty-state 下跨多种 restart/death 完成 | `DESIGN CANDIDATE ONLY`；不得据此恢复真实交易态 `SYNCED` |
| `orderId / permId / clientId / orderRef` | `orderId` 未必 account-unique；`clientId` 标识下单 client；`permId` 可为 0（外部成交）；`orderRef` 在订单生命周期关联；`execId` 每个 partial fill 不同，correction 只改最后句点后的部分 | 尚未产生订单事实 | `DOCUMENTED / NOT TESTED`；保留到另行授权的 paper-order 子阶段 |
| Completed orders | 官方记录 `reqCompletedOrders(apiOnly)` 与 `completedOrder`；当前页面未承诺 Read-Only 兼容，也未给出本项目可依赖的 completion 语义 | 10 秒无 completion；隔离复现稳定触发“需要 API 写权限”提示；无 `placeOrder`/`cancelOrder` | `OBSERVED - BLOCKED BY READ-ONLY POLICY`；不能解释成零条，不关闭保护追测 |
| Unknown / ambiguous broker facts | 官方没有把 timeout 或缺少 completion 定义为空集合或 ready | completed-orders/restart/current-time 均出现过无 completion | `PARTIAL - FAIL CLOSED`；deadline 后为 UNKNOWN，不恢复 `SYNCED` |
| Clean disconnect / reconnect | 官方提供 socket 状态与 broken-connection handling；重连逻辑由 client 实现 | 正常断开后同 ID 新只读会话成功，静态 snapshot 相同 | `DOCUMENTED AND OBSERVED - CLEAN ONLY`；reconnect 不是 reconciliation |
| Concurrent clients / Master | 每个 API connection 有 client ID；Master 可接收其他 API client 的状态，client 0 可接收 TWS/FIX 状态；一个 username 的 session/market-data 还受并发登录约束 | client 934/935 同时只读连接，空 snapshot 相同 | `DOCUMENTED AND OBSERVED - EMPTY ONLY`；非空跨 client 完整性未证 |
| Same client ID collision | error 326 表示 client ID 已被使用 | 第二个 client 937 收 326 并失败，第一个继续存活 | `DOCUMENTED AND OBSERVED` |
| Abrupt API client death / same-ID reconnect | 官方 broken-socket 页面未承诺 abrupt process death 后同 ID 的释放时间 | client 936 被强制终止，约 0.110 秒后首次同 ID 重连成功 | `OBSERVED ONLY`；不外推为时限保证 |
| Network disconnect / 1100/1101/1102 | 1100=Gateway/TWS 与 IB server 失联；1101=恢复但 market-data requests 丢失、需重提；1102=恢复且 requests 保持、无需重提 | 空状态与持三路订阅均见 1100→1102；production path 未重订且三路恢复；未见 1101 | `DOCUMENTED; 1100/1102 OBSERVED`；1101 保持未观察并被动采证，不反推 |
| Error/farm messages | 官方 error table 记录 10197 competing session、2103/2104、2105/2106、2107/2108、10225 bust 等语义 | 已见 10197、farm down/up；10225 对应代码路径已有测试 | `PARTIAL`；只把实际收到的码记为 observed |
| Gateway restart / End task / `TerminateProcess` | 官方只描述 socket/IB connectivity；不保证 Windows restart/kill 时序 | normal restart、Task Manager End task、精确 PID `TerminateProcess` 后均可重连并完成空 snapshot | `OBSERVED - EMPTY ONLY`；End task 不声称 hard kill，重连不等于恢复 |
| Windows Gateway running detection | 属 Windows process/CIM/listener 与本项目 detector 设计，不是 IB API 官方语义 | CIM Access Denied；`Get-Process`、4002 listener、只读 API server version 178 共同确认运行 | `OUT OF IB DOC SCOPE / OBSERVED`；查询不完整只能 `INDETERMINATE` |
| Order submit / ack / modify / cancel / fill / commission | 官方有订单与 execution callback 语义，但不构成本项目的运行证据或授权 | 零订单；代码路径未运行 | `DOCUMENTED / NOT TESTED`；当前只读 freeze 不覆盖，不授权订单 |

## 4. 官方来源索引

- Connectivity：[Establishing an API Connection](https://ibkrcampus.com/docs/tws-api/doc/connectivity/establishing-an-api-connection)、[Verify API Connection](https://ibkrcampus.com/docs/tws-api/doc/connectivity/verify-api-connection)、[Broken API Socket Connection](https://ibkrcampus.com/docs/tws-api/doc/connectivity/broken-api-socket-connection)、[Logging into Multiple Applications](https://ibkrcampus.com/docs/tws-api/doc/connectivity/logging-into-multiple-applications)
- Accounts/contracts：[Receive Managed Accounts](https://ibkrcampus.com/docs/tws-api/doc/account-portfolio-data/managed-accounts/receive-managed-accounts)、[Receiving Account Summary](https://ibkrcampus.com/docs/tws-api/doc/account-portfolio-data/account-summary/receiving-account-summary)、[Receive Positions](https://ibkrcampus.com/docs/tws-api/doc/account-portfolio-data/positions/receive-positions)、[Receive Contract Details](https://ibkrcampus.com/docs/tws-api/doc/contracts-financial-instruments/contract-details/receive-contract-details)
- Market data：[Market Data Type Behavior](https://ibkrcampus.com/docs/tws-api/doc/market-data-delayed/market-data-type-behavior)、[Request Real Time Bars](https://ibkrcampus.com/docs/tws-api/doc/market-data-live/5-second-bars/request-real-time-bars)、[Request Tick-by-Tick Data](https://ibkrcampus.com/docs/tws-api/doc/market-data-live/tick-by-tick-data/request-tick-by-tick-data)、[API Overnight Trading](https://www.interactivebrokers.com/campus/ibkr-quant-news/api-overnight-trading/)、[Current Time](https://ibkrcampus.com/docs/tws-api/doc/synchronous-api/current-time)、[Error Codes](https://ibkrcampus.com/docs/tws-api/ref/error-codes)、[System Message Codes](https://ibkrcampus.com/docs/tws-api/ref/system-message-codes)
- Orders/executions：[All Submitted Orders](https://ibkrcampus.com/docs/tws-api/doc/order-management/requesting-currently-active-orders/all-submitted-orders)、[API Client's Orders](https://ibkrcampus.com/docs/tws-api/doc/order-management/requesting-currently-active-orders/api-clients-orders)、[Open Orders](https://ibkrcampus.com/docs/tws-api/doc/order-management/open-orders)、[Request Execution Details](https://ibkrcampus.com/docs/tws-api/doc/order-management/execution-details/request-execution-details)、[Receive Execution Details](https://ibkrcampus.com/docs/tws-api/doc/order-management/execution-details/receive-execution-details)、[Request Completed Orders](https://ibkrcampus.com/docs/tws-api/doc/order-management/retrieving-completed-orders/requesting-completed-orders)、[Receive Completed Orders](https://ibkrcampus.com/docs/tws-api/doc/order-management/retrieving-completed-orders/receiving-completed-orders)、[Client ID 0 and Master Client ID](https://ibkrcampus.com/docs/tws-api/doc/order-management/client-id-0-and-the-master-client-id)、[Execution Object](https://ibkrcampus.com/docs/tws-api/ref/execution-class-reference/introduction)

## 5. 当前判定

Gate B2 已开始但**没有 PASS**。2026-08-18 Full-RTH v4 是 owner 接受的最终 health authority；原 v3 FAIL、Windows sidecar 历史 CRLF/摘要缺陷边界和 writer-lag OPEN 风险均保留。2026-08-20 已完成本矩阵的官方页面复核，并把所有旧待复核项转成明确的 documented、ambiguity、not-guaranteed 或 out-of-scope 结论。

当前可以进入的是“只读 B2 source/tests/docs/evidence exact-tree freeze”，其作用是封存和复查本阶段事实，不是把 `STATE.json.gate_b2` 改为 PASS，也不创建 paper/live order authorization。非空动态 reconciliation、订单身份、cross-client order visibility、submission ambiguity 和订单生命周期仍必须等待独立 paper-order 授权。
