# Gate B2：IB Gateway documented-vs-observed 矩阵

状态：**IN PROGRESS**  
范围：IB Gateway paper account；先只读，订单相关项目在明确进入 paper-order 子阶段前保持 `NOT TESTED`。

当前状态、证据索引和下一步见 [`GATE_B2_STATUS_20260810_ZH.md`](GATE_B2_STATUS_20260810_ZH.md)。

本文件只记录真实 Gateway 的直接观测。`FakeBroker`、单元测试或设计预期不能填入“已观测”列。官方文档依据尚未逐项复核的项目标为 `PENDING DOC REVIEW`，不得用记忆补齐。

| 项目 | 官方文档结论 | 真实 Gateway 观测 | 当前结论 | 证据 / 待办 |
|---|---|---|---|---|
| API TCP handshake | `PENDING DOC REVIEW` | Windows、paper port 4002、`readonly=True` 成功连接；server version 178 | `OBSERVED` | 2026-08-09 round 1 |
| Managed accounts | `PENDING DOC REVIEW` | 返回 1 个 managed account；报告只保存数量，不保存 account id | `OBSERVED` | 脱敏报告 `connection.account_count=1` |
| SPY contract qualification | `PENDING DOC REVIEW` | `SMART / ARCA / USD` 唯一解析，`conId=756733`，contract details 1 条 | `OBSERVED` | 2026-08-09 round 1 |
| Market-data entitlement | `PENDING DOC REVIEW` | `marketDataType=1`，无 entitlement-blocking error | `OBSERVED` | 休市观测，不代表流覆盖通过 |
| Overnight BidAsk / AllLast / 5s bars | `PENDING DOC REVIEW` | 周日休市采样均为 0；尚未在 overnight 窗口重跑 | `NOT TESTED IN OVERNIGHT` | 香港时间约 08:00 后明确标注 `OVERNIGHT` 重跑；不能替代 RTH |
| RTH BidAsk / AllLast / 5s bars | `PENDING DOC REVIEW` | 周日 15.234 秒采样均为 0 | `NOT TESTED IN RTH` | 香港时间约 21:30 后至少重跑 90 秒；休市 0 不作失败或成功推断 |
| Broker clock | `PENDING DOC REVIEW` | 7 个 RTT midpoint 样本，中位偏差 `+0.517s`，最大绝对值 `0.837s` | `OBSERVED` | 低于当前 2 秒阈值 |
| Repeated `reqCurrentTime` | `PENDING DOC REVIEW` | 0.2 秒间隔时至少一次 callback 未返回；`ib_async` 默认 `RequestTimeout=0` 导致同步调用无限等待。1.1 秒间隔下连续 7 次返回 | `OBSERVED - CLIENT HARDENED` | preflight 增加 1.1 秒间隔和 10 秒同步请求硬超时；12 tests PASS |
| Account summary | `PENDING DOC REVIEW` | `reqAccountSummary` 在 0.141 秒内完成，返回 71 项；报告只保存 count/hash | `OBSERVED` | 2026-08-10 safety-check report |
| Positions snapshot | `PENDING DOC REVIEW` | 三轮计数 `0 / 0 / 0`，canonical hash 相同 | `STATIC CANDIDATE ONLY` | 未覆盖持仓变化并发 |
| All-open-orders snapshot | `PENDING DOC REVIEW` | 三轮计数 `0 / 0 / 0`，canonical hash 相同 | `STATIC CANDIDATE ONLY` | 未覆盖订单 callback 并发或其他 client 的订单 |
| Executions snapshot | `PENDING DOC REVIEW` | 三轮计数 `0 / 0 / 0`，canonical hash 相同 | `STATIC CANDIDATE ONLY` | 未覆盖当日成交、late execution 或 correction |
| 双快照稳定屏障 | 设计候选；官方语义待复核 | 两个连续 pair 的整体及分项 hash 均相同；empty-state 下已跨 client death、Gateway restart、TerminateProcess 和 1100→1102 验证读取 completion | `STATIC CANDIDATE ONLY` | 仍不能据此恢复真实交易态 `SYNCED`；需非空动态 broker facts 实测 |
| `orderId / permId / clientId / orderRef` | `PENDING DOC REVIEW` | 尚未产生订单事实 | `NOT TESTED` | B2 paper-order 子阶段 |
| Completed orders | Read-Only API 开启时 order information 不可供 API 使用 | `reqCompletedOrders(False)` 10 秒无 completion；隔离最小复现稳定触发 Gateway 提示：“API客户端正在尝试发送需要API写入权限的请求”；复现代码无 `placeOrder`/`cancelOrder` | `BLOCKED BY READ-ONLY POLICY` | 截图 SHA-256 `63c4ecd5...98ddc`；证明 Gateway 的权限分类与拦截，不证明已提交订单；不得关闭 Read-Only |
| Unknown / ambiguous broker facts | `PENDING DOC REVIEW` | `reqCompletedOrders` 无 completion，以及 Gateway 重启时 handshake 后 snapshot 仍可能超时；均未被解释为空结果或 ready | `PARTIAL - READ PROTOCOL ONLY` | 已做 deadline / UNKNOWN / retry；尚未覆盖订单身份或 submission ambiguity |
| Clean client disconnect / reconnect | `PENDING DOC REVIEW` | 同一 clientId 正常断开后，新只读会话成功；server、account count、SPY conId 与静态 snapshot hash 一致 | `OBSERVED - CLEAN ONLY` | 不代表网络中断恢复；2026-08-09 reconnect report |
| Concurrent read-only clients | `PENDING DOC REVIEW` | clientId 934/935 同时连接成功，读取的静态 `0/0/0` snapshot hash 相同 | `OBSERVED - EMPTY STATE ONLY` | 不证明有订单时 cross-client visibility |
| Same client ID collision | 官方错误码 326：client ID 已被使用时拒绝连接 | 第一个 clientId 937 保持连接；第二个同 ID 连接收到 326 并失败；从未同时存活 | `OBSERVED` | 2026-08-10 client-fault v2 report |
| Abrupt API client death / same-ID reconnect | `PENDING DOC REVIEW` | clientId 936 进程未 disconnect 即被强制终止；约 0.110 秒后第一次同 ID 重连成功，静态快照相同 | `OBSERVED - CLIENT PROCESS ONLY` | 不等于 Gateway restart 或 IB server disconnect |
| Network disconnect / reconnect | `PENDING DOC REVIEW` | 对精确 `ibgateway.exe` 添加 45 秒 outbound block；Gateway 原 PID 存活、本地 API socket 保持连接；收到 2103/2157/1100，清理规则后收到 farm 恢复及 1102；恢复后 server time 与 `0/0/0` broker snapshot 完成且 hash 相同 | `OBSERVED - EMPTY STATE ONLY` | 2026-08-10 gateway-network-fault v2 report；规则已确认不存在；不证明动态非空 reconciliation |
| Gateway normal restart | `PENDING DOC REVIEW` | socket disconnect；重启期两次 10 秒 timeout 和 10141；同 clientId 第三次恢复；前后静态 `0/0/0` hash 相同 | `OBSERVED - EMPTY STATE ONLY` | 2026-08-10 gateway-restart v3 report |
| Gateway Task Manager End task | `PENDING DOC REVIEW` | socket disconnect；第一次恢复 10 秒 timeout 和 10141；第二次连接及完整 snapshot 成功；前后静态 hash 相同；应用表现出保存/退出过程 | `OBSERVED - NOT CLAIMED AS HARD KILL` | 2026-08-10 gateway-hard-kill v2 report |
| Gateway `TerminateProcess` | Windows `Stop-Process -Force` 对精确 PID 使用强制终止语义 | socket disconnect；前两次恢复各 10 秒 timeout，含 10141；第三次连接及完整 snapshot 成功；前后静态 hash 相同 | `OBSERVED - EMPTY STATE ONLY` | 2026-08-10 gateway-terminateprocess report |
| `1100 / 1101 / 1102` | 1100=Gateway/TWS 丢失 IB server 连接；1101/1102=恢复且分别表示 market data lost/maintained | Gateway 进程存活的外网阻断轮收到 1100，清理规则后收到 1102；没有收到 1101；1102 后 server time 和 broker snapshot 均成功 | `PARTIAL - 1100/1102 OBSERVED` | report SHA-256 `f8c88498...c8bc2`；1101 仍未直接观察，不得由 1102 推断 |
| Order submit / ack / modify / cancel / fill / commission | `PENDING DOC REVIEW` | 零订单；代码路径未运行 | `NOT TESTED` | 不属于当前只读轮次 |

## 当前判定

Gate B2 已开始，但 **没有 PASS**。截至 2026-08-10，已经直接观察基础连接、静态零事实快照、SPY 合约、LIVE entitlement、broker clock、client death / ID collision、Gateway restart / `TerminateProcess`，以及 Gateway 存活时的 1100 → 1102 与恢复后空状态 reconciliation completion。仍未证明 RTH stream、非空动态 snapshot barrier、1101、订单身份或任何订单生命周期。

周末、零订单、Read-Only 边界下有明显 B2 安全价值的主要 Gateway 实测已经完成。剩余实测依赖 overnight/RTH 市场时段，或者需要另行授权 paper-order 子阶段；`completed orders` 则保持被 Read-Only policy 阻断。当前证据已保存 digest，但工作树尚未形成最终 B2 Git exact-freeze。

详细轮次证据见 [`GATE_B2_READONLY_20260809.md`](GATE_B2_READONLY_20260809.md)。
