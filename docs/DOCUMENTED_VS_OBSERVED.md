# Gate B2：IB Gateway documented-vs-observed 矩阵

状态：**IN PROGRESS**  
范围：IB Gateway paper account；先只读，订单相关项目在明确进入 paper-order 子阶段前保持 `NOT TESTED`。

本文件只记录真实 Gateway 的直接观测。`FakeBroker`、单元测试或设计预期不能填入“已观测”列。官方文档依据尚未逐项复核的项目标为 `PENDING DOC REVIEW`，不得用记忆补齐。

| 项目 | 官方文档结论 | 真实 Gateway 观测 | 当前结论 | 证据 / 待办 |
|---|---|---|---|---|
| API TCP handshake | `PENDING DOC REVIEW` | Windows、paper port 4002、`readonly=True` 成功连接；server version 178 | `OBSERVED` | 2026-08-09 round 1 |
| Managed accounts | `PENDING DOC REVIEW` | 返回 1 个 managed account；报告只保存数量，不保存 account id | `OBSERVED` | 脱敏报告 `connection.account_count=1` |
| SPY contract qualification | `PENDING DOC REVIEW` | `SMART / ARCA / USD` 唯一解析，`conId=756733`，contract details 1 条 | `OBSERVED` | 2026-08-09 round 1 |
| Market-data entitlement | `PENDING DOC REVIEW` | `marketDataType=1`，无 entitlement-blocking error | `OBSERVED` | 休市观测，不代表流覆盖通过 |
| BidAsk / AllLast / 5s bars | `PENDING DOC REVIEW` | 周日 15.234 秒采样均为 0 | `NOT TESTED IN RTH` | 必须在完整或至少明确的 RTH 窗口重跑；休市 0 不作失败或成功推断 |
| Broker clock | `PENDING DOC REVIEW` | 7 个 RTT midpoint 样本，中位偏差 `+0.517s`，最大绝对值 `0.837s` | `OBSERVED` | 低于当前 2 秒阈值 |
| Repeated `reqCurrentTime` | `PENDING DOC REVIEW` | 0.2 秒间隔时至少一次 callback 未返回；`ib_async` 默认 `RequestTimeout=0` 导致同步调用无限等待。1.1 秒间隔下连续 7 次返回 | `OBSERVED - CLIENT HARDENED` | preflight 增加 1.1 秒间隔和 10 秒同步请求硬超时；10 tests PASS |
| Positions snapshot | `PENDING DOC REVIEW` | 三轮计数 `0 / 0 / 0`，canonical hash 相同 | `STATIC CANDIDATE ONLY` | 未覆盖持仓变化并发 |
| All-open-orders snapshot | `PENDING DOC REVIEW` | 三轮计数 `0 / 0 / 0`，canonical hash 相同 | `STATIC CANDIDATE ONLY` | 未覆盖订单 callback 并发或其他 client 的订单 |
| Executions snapshot | `PENDING DOC REVIEW` | 三轮计数 `0 / 0 / 0`，canonical hash 相同 | `STATIC CANDIDATE ONLY` | 未覆盖当日成交、late execution 或 correction |
| 双快照稳定屏障 | 设计候选；官方语义待复核 | 两个连续 pair 的整体及分项 hash 均相同 | `STATIC CANDIDATE ONLY` | 不能据此恢复真实交易态 `SYNCED`；需并发、断线和 restart 实测 |
| `orderId / permId / clientId / orderRef` | `PENDING DOC REVIEW` | 尚未产生订单事实 | `NOT TESTED` | B2 paper-order 子阶段 |
| Unknown / ambiguous broker facts | `PENDING DOC REVIEW` | 尚未制造真实未知事实 | `NOT TESTED` | 先完成只读可见性与 ownership 设计 |
| Clean client disconnect / reconnect | `PENDING DOC REVIEW` | 同一 clientId 正常断开后，新只读会话成功；server、account count、SPY conId 与静态 snapshot hash 一致 | `OBSERVED - CLEAN ONLY` | 不代表网络中断恢复；2026-08-09 reconnect report |
| Network disconnect / reconnect | `PENDING DOC REVIEW` | 尚未执行受控网络中断 | `NOT TESTED` | 下一轮只读故障测试 |
| Gateway restart | `PENDING DOC REVIEW` | 尚未执行 | `NOT TESTED` | 记录 restart 前后 snapshot、ID 和 callback |
| `1100 / 1101 / 1102` | `PENDING DOC REVIEW` | 本轮未出现 | `NOT TESTED` | 受控断线 / Gateway restart |
| Order submit / ack / modify / cancel / fill / commission | `PENDING DOC REVIEW` | 零订单；代码路径未运行 | `NOT TESTED` | 不属于当前只读轮次 |

## 当前判定

Gate B2 已开始，但 **没有 PASS**。2026-08-09 第一轮只证明基础连接、静态零事实快照、SPY 合约、LIVE entitlement 和 broker clock；没有证明 RTH stream、动态 snapshot barrier、重连、Gateway restart、订单身份或任何订单生命周期。

详细轮次证据见 [`GATE_B2_READONLY_20260809.md`](GATE_B2_READONLY_20260809.md)。
