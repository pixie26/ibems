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
| Market-data entitlement | `PENDING DOC REVIEW` | OVERNIGHT 与正式 `RTH+SMART` 均观察到 `marketDataType=1`；RTH v1 还观察到 competing-session 10197 | `OBSERVED` | 10197 后即使已有 ticks 也必须 fail-closed；stream coverage 分 session 判定 |
| Overnight routing | IBKR API 要求 overnight 市场数据使用 `exchange=OVERNIGHT`；它不与普通 SMART routed data 重合 | `OVERNIGHT+SMART` 120.219 秒为 `0/0/0`；改为明确 `OVERNIGHT+OVERNIGHT` 后立即获得三路数据 | `DOCUMENTED AND OBSERVED` | 错误 label/route 组合现已在连接前机械拒绝 |
| Overnight BidAsk / AllLast / 5s bars | overnight US stock/ETF session 从 Sunday 20:00 ET 开始；market data route 为 `OVERNIGHT` | 正确 route preflight 120.047 秒：`1620/13/25`；bounded Recorder 120.078 秒落盘：`923/16/25` | `OBSERVED - OVERNIGHT PASS` | report SHA `cda99091...fd7` / `7c97d75c...2e35`；不能替代 RTH |
| RTH BidAsk / AllLast / 5s bars | `PENDING DOC REVIEW` | 正式 preflight 120.109 秒：`25665/3168/25`；bounded Recorder 120.360 秒落盘：`15590/2843/25`，市场数据行均为 LIVE；2026-08-11 handler-count run 120.406 秒：handler/readback 均为 `8972/1707/25` | `OBSERVED - RTH BOUNDED PASS`（只是「三路拿到 LIVE 数据」；新实验另证明该窗口 handler→raw readback 无丢失，**仍不证明 IB/交易所端到端完整性**） | 新 report SHA `0cb4c95b...e02edc`。两组旧 preflight/Recorder 来自不重叠窗口，约 40% 的 BidAsk 差异不是有效损失率测量；按测量无效关闭，不再安排同步 A/B。见 [`GATE_B2_REVIEW_20260810.md`](GATE_B2_REVIEW_20260810.md) §3 |
| Broker clock | `PENDING DOC REVIEW` | 7 个 RTT midpoint 样本，中位偏差 `+0.517s`，最大绝对值 `0.837s` | `OBSERVED` | 低于当前 2 秒阈值 |
| Repeated `reqCurrentTime` | `PENDING DOC REVIEW` | 0.2 秒间隔时至少一次 callback 未返回；bounded Recorder v1 也在 10 秒 hard deadline 触发 `TimeoutError`。1.1 秒 request-before pacing 后 preflight 与 Recorder 均完成 | `OBSERVED - CLIENT HARDENED` | preflight 与 Recorder 统一 pacing；preflight 18 tests、clock 相关 3 tests PASS。**该 10 秒 deadline 当时属于 probe 脚本**：`IB.RequestTimeout` 默认 0（ib_async 语义为永不超时），只有 probe 自己设过它，`QuoteRecorder.run` 没有。已改为由 recorder 自身绑定，见 [`GATE_B2_REVIEW_20260810.md`](GATE_B2_REVIEW_20260810.md) §1 |
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
| Network disconnect with live subscriptions | `PENDING DOC REVIEW` | 真实 `QuoteRecorder.run()` 持 BidAsk/AllLast/BAR_5S 时收到 farm-down、1100→1102；connection epoch 保持 1、无 `RESUBSCRIBE_REQUIRED`；1102 后三路本地接收分别约 +0.264/+0.267/+0.896 秒 | `OBSERVED - 1102 PRODUCTION PATH` | 2026-08-12 部分日 artifact；没有 1101，不能验证 1101 重订；详见 [`GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md`](GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md) |
| Windows Gateway running detection | Windows CIM/process metadata 可能因权限不可读；官方语义待复核 | 真实 PID 19060 上 `Get-CimInstance Win32_Process` 返回 Access Denied；`Get-Process` 路径、`netstat` 的 `0.0.0.0:4002` listener 和只读 IB API server version 178 均确认 Gateway 运行 | `OBSERVED - DETECTOR HARDENED` | 查询失败只能是 `INDETERMINATE`/路径 unknown，不能解释成 `NOT_RUNNING`；危险程序级动作另需 `path_status=MATCH` |
| Gateway normal restart | `PENDING DOC REVIEW` | socket disconnect；重启期两次 10 秒 timeout 和 10141；同 clientId 第三次恢复；前后静态 `0/0/0` hash 相同 | `OBSERVED - EMPTY STATE ONLY` | 2026-08-10 gateway-restart v3 report |
| Gateway Task Manager End task | `PENDING DOC REVIEW` | socket disconnect；第一次恢复 10 秒 timeout 和 10141；第二次连接及完整 snapshot 成功；前后静态 hash 相同；应用表现出保存/退出过程 | `OBSERVED - NOT CLAIMED AS HARD KILL` | 2026-08-10 gateway-hard-kill v2 report |
| Gateway `TerminateProcess` | Windows `Stop-Process -Force` 对精确 PID 使用强制终止语义 | socket disconnect；前两次恢复各 10 秒 timeout，含 10141；第三次连接及完整 snapshot 成功；前后静态 hash 相同 | `OBSERVED - EMPTY STATE ONLY` | 2026-08-10 gateway-terminateprocess report |
| `1100 / 1101 / 1102` | 1100=Gateway/TWS 丢失 IB server 连接；1101/1102=恢复且分别表示 market data lost/maintained | 空状态 probe 与持三路订阅的 production `run()` 都收到 1100→1102；后者无重订且三路自动恢复。两轮均没有 1101 | `PARTIAL - 1100/1102 OBSERVED TWICE; 1101 NOT_RUN_ACCEPTED_NON_BLOCKER` | 1101 仍未直接观察，不得由 1102 推断。production 轮已覆盖“有订阅时 1102 保持订阅”，但没有进入“1101 requests lost”分支。**owner 于 2026-08-12 决定不再专门追 1101**（45 秒阻断只能产生 1102，取得该码需要长得多的断开，收益仅是把一条已有实现和单元测试的分支从未观察改为已观察）：代码路径保留，碰上真实 1101 时被动采集，任何未来断网实验都不以取码为目的。记录见 [`GATE_B2_STATUS_20260810_ZH.md`](GATE_B2_STATUS_20260810_ZH.md) §3.2 |
| Order submit / ack / modify / cancel / fill / commission | `PENDING DOC REVIEW` | 零订单；代码路径未运行 | `NOT TESTED` | 不属于当前只读轮次 |

## 当前判定

Gate B2 已开始，但 **没有 PASS**。截至 2026-08-12，已经直接观察基础连接、静态零事实快照、SPY 合约、LIVE entitlement、明确 OVERNIGHT destination 与正式 `RTH+SMART` 的三路行情及 Recorder raw-log 写盘、broker clock、client death / ID collision、Gateway restart / `TerminateProcess`、Gateway 存活时的 1100→1102，以及持三路订阅的 production 1100→1102 与三路恢复。仍未证明 Full-RTH 全日 Recorder health、非空动态 snapshot barrier、1101、订单身份或任何订单生命周期。新 incident 去重和 Gateway 检测修正有代码/测试证据；只有 Gateway 检测已追加真实环境验证，incident 新语义尚未在下一次真实 fault run 中验证。

零订单、Read-Only 边界下明显具有 B2 安全价值且无需额外故障授权的主要 Gateway 实测已经完成，包括 bounded OVERNIGHT 与 RTH。其他动态项目需要另行授权 paper-order 子阶段，`completed orders` 则保持被 Read-Only policy 阻断。当前证据已保存 digest，但工作树尚未形成最终 B2 Git exact-freeze。

详细轮次证据见 [`GATE_B2_READONLY_20260809.md`](GATE_B2_READONLY_20260809.md)。
