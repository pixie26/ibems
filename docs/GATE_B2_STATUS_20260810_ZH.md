# Gate B2 当前状态摘要（2026-08-10）

截止时间：`2026-08-10 23:08 HKT`（`2026-08-10 15:08 UTC`）。

## 1. 当前正式结论

- **Gate B1：PASS**，绑定 exact-freeze commit `117188cea53906665739af3775af64d156856f41`。
- **Gate B2：READ-ONLY IN PROGRESS，尚未 PASS。**
- 当前只允许 IB Gateway paper account 的只读协议验证；没有发送订单，也没有获得 paper-order 或 live-order 授权。
- Gateway 的 Read-Only API 保护保持开启。不能为了补齐 `completed orders` 而关闭该保护。
- 在零订单、Gateway Read-Only 边界内，当前有明显 B2 安全价值且不需要额外故障授权的主要 Gateway 实测已经完成。
- SPY overnight 与正式 `RTH+SMART` 行情及 bounded Recorder 均已完成并 PASS。RTH 首轮还直接暴露了 competing-session `10197` 和报告判定缺口；修正后独立 v2 才作为通过证据。

`STATE.json` 是 Gate B1 的机器可读权威状态；其中 `gate_b2=NOT_STARTED` 尚未纳入本轮人工执行的真实 Gateway 证据，且该文件标明为生成文件，因此不得手工修改。本摘要和 [`GATE_B2_READONLY_20260809.md`](GATE_B2_READONLY_20260809.md) 记录当前 B2 实验状态；最终 B2 freeze 时需要统一机器状态、源码、文档和证据。

## 2. 已完成、部分完成和未完成

| 项目 | 状态 | 当前能得出的结论 | 明确边界 |
|---|---|---|---|
| API connectivity / preflight | 已完成 | paper port 4002、`readonly=True` 连接成功；server version 178 | 不授权订单 |
| Managed account / account summary | 已完成 | 1 个 managed account；`reqAccountSummary` 完成并返回 71 项 | 报告只保存 count/hash；未保存账户名或余额明细 |
| SPY qualification | 已完成 | `SMART / ARCA / USD` 与 `OVERNIGHT / ARCA / USD` 均唯一解析，`conId=756733` | route 必须与 session label 明确匹配 |
| LIVE entitlement | 已完成 | OVERNIGHT 与正式 `RTH+SMART` 均观察到 `marketDataType=1` | entitlement 仍可能受 competing session 影响，10197 必须 fail-closed |
| Positions / all-open-orders / executions | 已完成，空状态限定 | 多轮均为 `0 / 0 / 0`，相邻 snapshot hash 一致 | 只证明空状态读取 completion，不证明动态原子性 |
| Completed orders | 被安全策略阻断 | 10 秒内无 completion，记为 `UNKNOWN`；最小复现触发 Gateway Read-Only 提示 | 不能解释成零条；不得关闭 Read-Only 追测 |
| 多只读 client 空状态可见性 | 已完成 | client 934/935 同时连接并读取相同 `0 / 0 / 0` snapshot | 不证明有订单时的 cross-client visibility |
| 相同 client ID 冲突 | 已完成 | 第二个同 ID 连接收到错误 326；不存在两个同时存活的同 ID session | 尚无订单身份事实 |
| API client 异常死亡 / 同 ID 重连 | 已完成 | client 936 未正常 disconnect 即被强制终止；约 0.110 秒后第一次同 ID 重连成功 | 只覆盖 client 进程死亡 |
| 请求无 completion 的 fail-closed | 部分完成 | `reqCurrentTime`、completed-orders 和 restart snapshot 的无 completion 均有硬 deadline，不被解释为空结果或 ready | 尚未覆盖订单提交结果不明确 |
| Gateway 正常 restart | 已完成，空状态限定 | socket 断开后，同 client ID 重新连接并完成 broker snapshot | 仅连接成功不足以判定 ready/synced |
| Task Manager End task | 已完成，空状态限定 | 重启后完整 snapshot 成功 | Gateway 表现出保存/退出过程，不声称为 hard kill |
| Windows `TerminateProcess` | 已完成，空状态限定 | 强制终止后重启；第三次恢复尝试完成连接和 snapshot | 不证明非空 reconciliation |
| Gateway 存活时受控外网中断 | 已完成，空状态限定 | 观察到真实 `1100 -> 1102`；恢复后 server time 与 snapshot 完成 | 没有观察到 1101；不证明动态订单 callback |
| SPY overnight 三路行情 / Recorder | 已完成 | 正确 `OVERNIGHT` route：preflight `1620/13/25`；落盘 Recorder `923/16/25`，两轮均 PASS | 只证明 overnight；不是 RTH 或 Full-RTH health report |
| SPY RTH BidAsk / AllLast / 5s bars / Recorder | 已完成 | preflight 120.109 秒 `25665/3168/25`；Recorder 120.360 秒落盘 `15590/2843/25`，均为 LIVE | bounded 两分钟证据；不是 Full-RTH 全日 health |
| RTH handler→raw readback 一致性 | 已完成 | 2026-08-11 `8972/1707/25` handler counts 与 raw readback 逐项相等 | 只关闭该窗口 handler 后写路径丢失；callback 前差异仍需同步 A/B |
| 非空动态 reconciliation | 未完成 | 当前没有仓位、挂单或成交事实 | 需要另行授权 paper-order 子阶段 |
| `orderId / permId / clientId / orderRef` | 未完成 | 尚未产生真实订单身份事实 | 需要另行授权 paper-order 子阶段 |
| submit / ack / modify / cancel / fill / commission / late callback | 未完成 | 订单路径没有运行 | 不属于当前只读轮次 |

## 3. 只读实测的剩余边界（2026-08-11 复核）

原轮次遗漏了一个高价值只读场景：外网中断 probe 没有持有行情订阅，因此没有覆盖
“连接恢复但既有订阅已失效”的静默失败族。后续仍保持零 broker write，但应在活跃行情期间
持有三路订阅，直接记录 recovery code、per-stream staleness 和 resubscribe 结果。改进 probe
已实现但尚未执行；它也不等于 production `QuoteRecorder.run()` 的真实 fault 验证。

1. `completed orders` 被 Gateway Read-Only policy 阻断；继续需要降低保护，不接受。
2. overnight 与 RTH bounded 行情均已完成；仍未运行 Full-RTH 全日 health。
3. 不应反复断网碰运气；持订阅实验也不得预设必得 1101。官方语义只支持按实际结果区分
   1101（requests lost，需要重订阅）与 1102（requests recovered），不得互相推断。
4. 非空 broker facts、订单身份、cross-client order visibility 和订单 callback 必须先获得 paper-order 独立授权。

现在仍可开展官方文档复核、证据索引整理、代码审查和 freeze 准备；持订阅 fault injection
虽不产生订单，但会中断 Gateway 外网连接，执行前仍应由 operator 单独确认窗口。

## 4. 已封存的本地证据索引

| 场景 | 报告 / 证据 | SHA-256 |
|---|---|---|
| 第一轮只读 preflight | `artifacts/ib_preflight/20260809_b2_round1/report.json` | `e37ea03c9c48b91426ced9c9b6d6bd41979778924ba90fc96bfdb28d8788f3cc` |
| 干净断开后重连 | `artifacts/ib_preflight/20260809_b2_round1_reconnect/report.json` | `23b4503426dfae90c1df25692d9db16d3dc813e0843486d5e11573d6b03d6d21` |
| completed-orders Read-Only 提示截图 | `artifacts/ib_preflight/20260810_readonly_completed_orders_prompt/gateway_readonly_prompt.png` | `63c4ecd568d97563949cdc43011020b273eb243f9daa9f9bc58ec3f3ceb98ddc` |
| 提示后安全复核 | `artifacts/ib_preflight/20260810_b2_post_prompt_safety_check/report.json` | `7b8d0e5fe1491feed70a7b1b7578eea8da4f597b05ed021e07132e48c576cede` |
| 多 client / client death | `artifacts/ib_preflight/20260810_b2_client_fault_v2/report.json` | `a20f51f843330ef67a2f8ba955059905980ee2a19a94e0338824bd26e59c61ac` |
| Gateway 正常 restart | `artifacts/ib_preflight/20260810_b2_gateway_restart_v3/report.json` | `90fc489e55d23e69f7295827950945e85c3f25b52d8d2c6607600afefc633329` |
| Task Manager End task | `artifacts/ib_preflight/20260810_b2_gateway_hard_kill_v2/report.json` | `b954367be4f976acf3ffca02eb7e4e71bb82eac1dadb609b5a64128ea5b4c441` |
| Windows `TerminateProcess` | `artifacts/ib_preflight/20260810_b2_gateway_terminateprocess/report.json` | `2097c4fb5461b15a523879ee4cc7b7765e204db4d38d921a9a8b0e4c457a52d8` |
| Gateway 存活时外网中断 | `artifacts/ib_preflight/20260810_b2_gateway_network_fault_v2/report.json` | `f8c88498a74402d59a49f80a0cdf9b61903a8d361b80b7045457f467f30c8bc2` |
| SPY OVERNIGHT 三路 preflight | `artifacts/ib_preflight/20260810_b2_overnight_market_v2/report.json` | `cda99091b758cbd8fbe442d27bc5132199d6530ac118c94f74be25c8fb202fd7` |
| SPY OVERNIGHT bounded Recorder | `artifacts/ib_preflight/20260810_b2_overnight_recorder_v2/report.json` | `7c97d75c9da2ac6372f241f721a6038ff4dea326acab869fc3268948869b2e35` |
| SPY RTH v1（10197，失败证据） | `artifacts/ib_preflight/20260810_b2_rth_market_v1/report.json` | `db640ef3fec57adadac81426b314afcc0cc28ef31cb70f9243ab9d3989d34143` |
| SPY RTH preflight v2 | `artifacts/ib_preflight/20260810_b2_rth_market_v2/report.json` | `204c979b8c29670a33dfdba111e704f51d6971dddc6989b4214ac0e8ad914c1b` |
| SPY RTH bounded Recorder | `artifacts/ib_preflight/20260810_b2_rth_recorder_v1/report.json` | `86de43714c1d9fa1f515d01ddc126589b11e1634ff23f1ef57ca439cfa9a543d` |
| SPY RTH handler-count Recorder | `artifacts/ib_preflight/20260811_b2_rth_recorder_handler_counts_v1/report.json` | `0cb4c95b86d39e054b3c384bf8a225958609cf4c5dff167b49177ed9c4e02edc` |

上述是本地 evidence bundle；网络中断目录的 `SHA256SUMS` 已复核。当前工作树仍有未提交的 B2 脚本和文档修改，因此这里的“封存”表示证据文件及其 digest 已保存，**不表示 B2 已形成最终 Git exact-freeze**。

## 5. 后续计划与进入下一阶段的条件

1. SPY overnight 行情/Recorder 已完成；详细过程见 [`GATE_B2_OVERNIGHT_20260810.md`](GATE_B2_OVERNIGHT_20260810.md)。
2. 正式 RTH 三路行情与 bounded Recorder 已完成；详细过程见 [`GATE_B2_RTH_20260810.md`](GATE_B2_RTH_20260810.md)。
3. 逐项完成官方 IB 文档复核，并更新 [`DOCUMENTED_VS_OBSERVED.md`](DOCUMENTED_VS_OBSERVED.md)。
4. 修复或明确 Windows full-suite/provenance gap，整理 B2 source、tests、docs 和 evidence，形成新的可复查 freeze；不得借用 B1 exact-freeze 为新代码背书。
5. 只读证据完成并封存后，由 owner **单独决定**是否授权“paper account、1 股 SPY、机械订单生命周期”的 paper-order protocol。
6. 只有进入该子阶段后，才验证非空 reconciliation、订单身份、跨 client 订单可见性、submit ambiguity、modify/cancel/fill、Gateway restart 和 late/duplicate/out-of-order callback。

## 6. 文档导航

- 本文：当前状态、边界、证据索引和下一步。
- [`GATE_B2_OVERNIGHT_20260810.md`](GATE_B2_OVERNIGHT_20260810.md)：正确 overnight routing、三路行情及 bounded Recorder 写盘证据。
- [`GATE_B2_RTH_20260810.md`](GATE_B2_RTH_20260810.md)：RTH competing-session 失败、判定加固、正式三路行情及 bounded Recorder 写盘证据。
- [`GATE_B2_READONLY_20260809.md`](GATE_B2_READONLY_20260809.md)：每一轮真实 Gateway 测试的详细过程与观测。
- [`DOCUMENTED_VS_OBSERVED.md`](DOCUMENTED_VS_OBSERVED.md)：官方文档语义与真实 Gateway 直接观测的逐项矩阵。
- [`GATE_B1_SIGNOFF_117188cea539.md`](GATE_B1_SIGNOFF_117188cea539.md)：Gate B1 exact-freeze owner acceptance；其范围不包含真实 IB 行为。
