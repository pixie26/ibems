# Gate B2 当前状态摘要（2026-08-10）

初始截止时间：`2026-08-10 23:08 HKT`；本摘要更新至 `2026-08-12`。

## 1. 当前正式结论

- **Gate B1：PASS**，绑定 exact-freeze commit `117188cea53906665739af3775af64d156856f41`。
- **Gate B2：READ-ONLY IN PROGRESS，尚未 PASS。**
- 当前只允许 IB Gateway paper account 的只读协议验证；没有发送订单，也没有获得 paper-order 或 live-order 授权。
- Gateway 的 Read-Only API 保护保持开启。不能为了补齐 `completed orders` 而关闭该保护。
- 在零订单、Gateway Read-Only 边界内，当前有明显 B2 安全价值且不需要额外故障授权的主要 Gateway 实测已经完成。
- SPY overnight 与正式 `RTH+SMART` 行情及 bounded Recorder 均已完成并 PASS。RTH 首轮还直接暴露了 competing-session `10197` 和报告判定缺口；修正后独立 v2 才作为通过证据。
- 2026-08-12 已在真实 `QuoteRecorder.run()` 持有三路订阅时执行一次经授权的 45 秒 outbound block，
  直接观察 1100→1102、不重订阅和三路自动恢复；1101 仍未观察。该进程约 13:00 ET 才启动，属于部分日
  长跑，不是 Full-RTH。

`STATE.json` 是机器可读权威状态；当前为 `gate_b2=READ_ONLY_IN_PROGRESS`、`order_authorization=NONE`。该文件是生成文件，不得手工修改。本摘要和 [`GATE_B2_READONLY_20260809.md`](GATE_B2_READONLY_20260809.md) 记录当前 B2 实验状态；最终 B2 freeze 时仍需统一机器状态、源码、文档和证据。

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
| 持三路订阅受控断网 + production `run()` | 部分完成 | 真实 1100→1102；connection epoch 保持 1；未重订；1102 后 BidAsk/AllLast/BAR_5S 分别约 0.264/0.267/0.896 秒恢复本地接收 | 未观察 1101；新 incident 去重代码是在本轮后加入，尚未真实复测 |
| SPY overnight 三路行情 / Recorder | 已完成 | 正确 `OVERNIGHT` route：preflight `1620/13/25`；落盘 Recorder `923/16/25`，两轮均 PASS | 只证明 overnight；不是 RTH 或 Full-RTH health report |
| SPY RTH BidAsk / AllLast / 5s bars / Recorder | 已完成 | preflight 120.109 秒 `25665/3168/25`；Recorder 120.360 秒落盘 `15590/2843/25`，均为 LIVE | bounded 两分钟证据；不是 Full-RTH 全日 health |
| RTH handler→raw readback 一致性 | 已完成 | 2026-08-11 `8972/1707/25` handler counts 与 raw readback 逐项相等 | 直接关闭该窗口 handler 后写路径丢失；旧顺序窗口约 40% 差异不是有效测量，不再安排同步 A/B |
| Liveness incident 去重 | 代码/测试完成 | poll 级重复 marker 改为 START/UPDATE/60s CHECKPOINT/END；恢复 END 必须晚于 incident 后首个 BAR_5S | 旧真实 run 有 380 条重复 marker；新实现尚未在真实 Gateway fault 上复测 |
| Windows Gateway 存活检测 | 已修复并实测 | CIM Access Denied 时继续用 `Get-Process`、`netstat` listener 和只读 API；现场返回 `RUNNING_API_VERIFIED`、server version 178 | 危险程序级动作仍额外要求 `path_status=MATCH`；查询不完整只能是 `INDETERMINATE` |
| Windows provenance 重新生成 | 已修复 | `STATE.json` 用 UTF-8 bytes + LF 写入；实测 `CR=0` 且 `provenance --check` 通过 | 不改变 B1 historical freeze，也不为当前 B2 worktree 提供新 attestation |
| 非空动态 reconciliation | 未完成 | 当前没有仓位、挂单或成交事实 | 需要另行授权 paper-order 子阶段 |
| `orderId / permId / clientId / orderRef` | 未完成 | 尚未产生真实订单身份事实 | 需要另行授权 paper-order 子阶段 |
| submit / ack / modify / cancel / fill / commission / late callback | 未完成 | 订单路径没有运行 | 不属于当前只读轮次 |

## 3. 只读实测的剩余边界（2026-08-12 复核）

原最高优先级“持三路订阅受控断网 + production `run()`”已经执行，详细见
[`GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md`](GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md)。本轮取得
1100→1102 而不是 1101：验证了 1102 不重订与恢复后三路增量，但不能关闭 1101→重订阅分支。
本轮还暴露旧实现按 0.25 秒 poll 重复写 380 条 marker；新 incident 生命周期已通过本地测试，但运行中
PID 18488 加载的是旧代码，因此必须把“真实旧问题”和“新代码离线修正”分开。

1. `completed orders` 被 Gateway Read-Only policy 阻断；继续需要降低保护，不接受。
2. overnight 与 RTH bounded 行情均已完成；当前约 13:00 ET 起跑的长跑是部分日，仍未运行 Full-RTH
   全日 health。该全日轮次还需
   同时验证开盘 cross、午盘、收盘 cross 的 bar cadence 与长期资源/队列水位。
3. 不应反复断网碰运气；持订阅实验也不得预设必得 1101。官方语义只支持按实际结果区分
   1101（requests lost，需要重订阅）与 1102（requests recovered），不得互相推断。
4. 非空 broker facts、订单身份、cross-client order visibility 和订单 callback 必须先获得 paper-order 独立授权。

现在仍可开展当前部分日运行的最终封存、官方文档复核、证据索引整理、代码审查和 freeze 准备。不得为
碰取 1101 重复断网；任何新的 fault injection 仍需 operator 对该次精确目标和窗口重新授权。

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
| generic tick 49 error 321 失败轮 | `artifacts/ib_preflight/20260812_controlled_disconnect_real_run/` | 已完成失败 manifest；尚未纳入最终 evidence snapshot |
| 持订阅断网 + 部分日 production run | `artifacts/ib_preflight/20260812_controlled_disconnect_real_run_retry_empty_generic_ticks/` | 仍在写入，**不得提前给最终 digest/PASS** |

表中 2026-08-11 及以前的项目是已有本地 evidence bundle；网络中断目录的 `SHA256SUMS` 已复核。
最后两个 2026-08-12 artifact 另行标为“失败轮已完成但未纳入 snapshot”和“仍在写入”，不属于已封存证据。
这里的“封存”只表示对应证据文件及 digest 已保存，**不表示 B2 已形成最终 Git exact-freeze**；新的
source、tests、docs 与后续真实 Gateway 证据仍需在最终 freeze 中统一绑定。

## 5. 后续计划与进入下一阶段的条件

1. SPY overnight 行情/Recorder 已完成；详细过程见 [`GATE_B2_OVERNIGHT_20260810.md`](GATE_B2_OVERNIGHT_20260810.md)。
2. 正式 RTH 三路行情与 bounded Recorder 已完成；详细过程见 [`GATE_B2_RTH_20260810.md`](GATE_B2_RTH_20260810.md)。
3. 当前部分日 Recorder 自然结束后，核对并封存最终 writer accounting、health、manifest 和 digest；不能
   把它升级为 Full-RTH。
4. 用加载新 incident 代码的 Recorder 运行一次非破坏性长窗口；若未来再次做 fault injection，必须重新
   获得精确授权。1101 保持“未观察”，不为取码反复断网。
5. 另一天从开盘前运行一次 Full-RTH 全日 health，同时闭环 `finalize_day`、全日 bar cadence 和长期资源行为。
6. 完成 Recorder 强杀后的 gzip 段级完整性/不完整尾段处置，以及 attestation 统一读取 Git 对象。
7. 逐项完成官方 IB 文档复核，整理 B2 source、tests、docs 和 evidence，形成新的可复查 freeze；不得借用 B1 exact-freeze 为新代码背书。
8. 只读证据完成并封存后，由 owner **单独决定**是否授权“paper account、1 股 SPY、机械订单生命周期”的 paper-order protocol。只有进入该子阶段后，才验证非空 reconciliation、订单身份、跨 client 订单可见性、submit ambiguity、modify/cancel/fill、Gateway restart 和 late/duplicate/out-of-order callback；live order 继续禁止。

## 6. 文档导航

- 本文：当前状态、边界、证据索引和下一步。
- [`GATE_B2_OVERNIGHT_20260810.md`](GATE_B2_OVERNIGHT_20260810.md)：正确 overnight routing、三路行情及 bounded Recorder 写盘证据。
- [`GATE_B2_RTH_20260810.md`](GATE_B2_RTH_20260810.md)：RTH competing-session 失败、判定加固、正式三路行情及 bounded Recorder 写盘证据。
- [`GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md`](GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md)：持三路订阅的 production 断网、旧 marker 重复、incident 修正和 Gateway 检测修复。
- [`GATE_B2_READONLY_20260809.md`](GATE_B2_READONLY_20260809.md)：每一轮真实 Gateway 测试的详细过程与观测。
- [`DOCUMENTED_VS_OBSERVED.md`](DOCUMENTED_VS_OBSERVED.md)：官方文档语义与真实 Gateway 直接观测的逐项矩阵。
- [`GATE_B1_SIGNOFF_117188cea539.md`](GATE_B1_SIGNOFF_117188cea539.md)：Gate B1 exact-freeze owner acceptance；其范围不包含真实 IB 行为。
