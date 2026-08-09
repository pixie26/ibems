# Gate B2 当前状态摘要（2026-08-10）

截止时间：`2026-08-10 01:09 HKT`（`2026-08-09 17:09 UTC`）。

## 1. 当前正式结论

- **Gate B1：PASS**，绑定 exact-freeze commit `117188cea53906665739af3775af64d156856f41`。
- **Gate B2：READ-ONLY IN PROGRESS，尚未 PASS。**
- 当前只允许 IB Gateway paper account 的只读协议验证；没有发送订单，也没有获得 paper-order 或 live-order 授权。
- Gateway 的 Read-Only API 保护保持开启。不能为了补齐 `completed orders` 而关闭该保护。
- 在“周末、零订单、Gateway Read-Only”边界内，当前有明显 B2 安全价值且可以安全执行的主要 Gateway 实测已经完成。
- 下一项实际测试是香港时间约 `08:00` 后的 SPY overnight 行情/Recorder；正式 RTH 三路行情在约 `21:30` 后测试。

`STATE.json` 是 Gate B1 的机器可读权威状态；其中 `gate_b2=NOT_STARTED` 尚未纳入本轮人工执行的真实 Gateway 证据，且该文件标明为生成文件，因此不得手工修改。本摘要和 [`GATE_B2_READONLY_20260809.md`](GATE_B2_READONLY_20260809.md) 记录当前 B2 实验状态；最终 B2 freeze 时需要统一机器状态、源码、文档和证据。

## 2. 已完成、部分完成和未完成

| 项目 | 状态 | 当前能得出的结论 | 明确边界 |
|---|---|---|---|
| API connectivity / preflight | 已完成 | paper port 4002、`readonly=True` 连接成功；server version 178 | 不授权订单 |
| Managed account / account summary | 已完成 | 1 个 managed account；`reqAccountSummary` 完成并返回 71 项 | 报告只保存 count/hash；未保存账户名或余额明细 |
| SPY qualification | 已完成 | `SMART / ARCA / USD` 唯一解析，`conId=756733` | 不等于行情流已经通过 |
| LIVE entitlement | 已完成 | `marketDataType=1`，未出现 entitlement-blocking error | 休市观测不能证明三路 stream |
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
| SPY overnight 三路行情 / Recorder | 未完成 | 尚未形成 overnight 证据 | 等香港时间约 08:00 后；必须明确标注 `OVERNIGHT` |
| SPY RTH BidAsk / AllLast / 5s bars | 未完成 | 周日采样为零，不作成功或失败推断 | 等香港时间约 21:30 后，至少采样 90 秒 |
| 非空动态 reconciliation | 未完成 | 当前没有仓位、挂单或成交事实 | 需要另行授权 paper-order 子阶段 |
| `orderId / permId / clientId / orderRef` | 未完成 | 尚未产生真实订单身份事实 | 需要另行授权 paper-order 子阶段 |
| submit / ack / modify / cancel / fill / commission / late callback | 未完成 | 订单路径没有运行 | 不属于当前只读轮次 |

## 3. “周末还能不能继续测”的结论

在当前安全边界下，**没有发现一个明显的、现在即可安全实测但被遗漏的关键周末 Gateway 场景**。当前不能继续完成的项目均有明确原因：

1. `completed orders` 被 Gateway Read-Only policy 阻断；继续需要降低保护，不接受。
2. overnight 和 RTH 行情分别需要对应的市场时段。
3. 1101 不应通过反复断网碰运气；后续可在活跃行情订阅期间观察，但不得从已观察的 1102 推断 1101。
4. 非空 broker facts、订单身份、cross-client order visibility 和订单 callback 必须先获得 paper-order 独立授权。

现在仍可开展官方文档复核、证据索引整理、代码审查和 freeze 准备；这些属于技术审查，不是新的 Gateway 行为实测。

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

上述是本地 evidence bundle；网络中断目录的 `SHA256SUMS` 已复核。当前工作树仍有未提交的 B2 脚本和文档修改，因此这里的“封存”表示证据文件及其 digest 已保存，**不表示 B2 已形成最终 Git exact-freeze**。

## 5. 后续计划与进入下一阶段的条件

1. 香港时间约 `08:00` 后运行一次 SPY overnight 行情/Recorder，明确标注 `OVERNIGHT`；记录 BidAsk、AllLast、5s bars、market-data type、错误码、时间范围和证据 digest。
2. 香港时间约 `21:30` 后运行正式 RTH 三路行情验证，至少 90 秒，要求三路事件计数均非零；overnight 结果不能替代 RTH。
3. 逐项完成官方 IB 文档复核，并更新 [`DOCUMENTED_VS_OBSERVED.md`](DOCUMENTED_VS_OBSERVED.md)。
4. 修复或明确 Windows full-suite/provenance gap，整理 B2 source、tests、docs 和 evidence，形成新的可复查 freeze；不得借用 B1 exact-freeze 为新代码背书。
5. 只读证据完成并封存后，由 owner **单独决定**是否授权“paper account、1 股 SPY、机械订单生命周期”的 paper-order protocol。
6. 只有进入该子阶段后，才验证非空 reconciliation、订单身份、跨 client 订单可见性、submit ambiguity、modify/cancel/fill、Gateway restart 和 late/duplicate/out-of-order callback。

## 6. 文档导航

- 本文：当前状态、边界、证据索引和下一步。
- [`GATE_B2_READONLY_20260809.md`](GATE_B2_READONLY_20260809.md)：每一轮真实 Gateway 测试的详细过程与观测。
- [`DOCUMENTED_VS_OBSERVED.md`](DOCUMENTED_VS_OBSERVED.md)：官方文档语义与真实 Gateway 直接观测的逐项矩阵。
- [`GATE_B1_SIGNOFF_117188cea539.md`](GATE_B1_SIGNOFF_117188cea539.md)：Gate B1 exact-freeze owner acceptance；其范围不包含真实 IB 行为。

