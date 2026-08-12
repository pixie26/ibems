# Gate B2 当前状态摘要（2026-08-10）

初始截止时间：`2026-08-10 23:08 HKT`；本摘要更新至 `2026-08-13`。

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

- **2026-08-13 Amendment：原“当前 `main` 从未接触过 Gateway”的状态已失效。** commit `83e9573` 已于
  2026-08-12 运行一次 Full-RTH 尝试：约 66 分钟后出现三路静默，`RecoveryScheduler` 在 durable event
  明确记录 `evidence_of_life=True` 时仍错误选择 full reconnect，随后新行情请求收到 `10197`，最终 health
  FAIL、覆盖约 17%。事故后独立 120 秒 probe 又取得三路 LIVE，说明不是持久 entitlement/route 失败。
  详细事实、owner“没有主动登录真实账户”的明确陈述、API log 边界及整改条件见
  [`INCIDENT_FULL_RTH_20260812_ZH.md`](INCIDENT_FULL_RTH_20260812_ZH.md)。Full-RTH 仍未完成。

`STATE.json` 是机器可读权威状态；当前为 `gate_b2=READ_ONLY_IN_PROGRESS`、`order_authorization=NONE`、
`gate_b1_covers_worktree=false`——即当前这棵树不在任何 attestation 覆盖范围内，B2 全部证据都是在未覆盖
的树上采集的，只读阶段可接受，但 B2 收口前必须有一次覆盖当前树的新 freeze。该文件是生成文件，不得手工
修改。本摘要和 [`GATE_B2_READONLY_20260809.md`](GATE_B2_READONLY_20260809.md) 记录当前 B2 实验状态；最终
B2 freeze 时仍需统一机器状态、源码、文档和证据。

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
| SPY Full-RTH 全日 health | **失败，未完成** | commit `83e9573` 从开盘起取得约 66 分钟三路 LIVE；随后 `evidence_of_life=True` 时错误 full reconnect，新请求收到 `10197`，health FAIL、覆盖约 17% | 失败证据保留；不得把前缀非零升级为 Full-RTH PASS；见事故报告 |
| RTH handler→raw readback 一致性 | 已完成 | 2026-08-11 `8972/1707/25` handler counts 与 raw readback 逐项相等 | 直接关闭该窗口 handler 后写路径丢失；旧顺序窗口约 40% 差异不是有效测量，不再安排同步 A/B |
| Liveness incident 去重 | 代码/测试完成 | poll 级重复 marker 改为 START/UPDATE/60s CHECKPOINT/END；恢复 END 必须晚于 incident 后首个 BAR_5S | 旧真实 run 有 380 条重复 marker；新实现尚未在真实 Gateway fault 上复测 |
| 停牌态 generic tick 49 | 已证伪，不可得 | Gateway 对 STK 返回 error 321，整个 `reqMktData` 拿不到 LIVE 回调、三路归零 | 默认已改回不请求（`market_data_generic_ticks=""`）；抑制器因此无输入，manifest 用 `halt_state_available` 明示，读者不得把"无 halt marker"读成"未停牌" |
| 未解释静默的恢复策略 | **发现生产默认组合缺陷，待修复** | 设计要求有生命迹象时只做最小修复，但 `plan()` 先消耗 fast full-reconnect；真实 run 已记录 `evidence_of_life=True` 仍 full reconnect | 测试使用 `fast_attempts=0` 隐藏了默认值缺陷；修复和真实 smoke 前不得再次投入 Full-RTH |
| 丢覆盖时的 health 真实性 | 代码/测试完成 | `FEED_OUTAGE`/`GAP_SUSPECTED` 及收盘时未闭合的 incident 均使 `health_ok=false`；`EXPECTED_SILENCE` 单独不失败 | 这是"不退出"的配套约束：进程不再用退出报警，改由 health 报警 |
| Parquet 内容保真 | 已完成（离线） | 分级价格、超 2^53 纳秒墙钟、带偏移与微秒的 broker timestamp、`special_conditions` 反解、跨段 event_id 顺序、空字段保持 null，逐值相等 | 此前只验行数与 schema，二者在四舍五入/丢时区/乱序下均会通过；本项用合成但真实形态的 tick，不替代真实 tick 复核 |
| 强杀后重启续跑 | 已完成（离线） | 强杀 → 后继取新 run_id 续录 → `finalize_day` 折叠为单文件，4 行 / 2 个 run_id，且该日仍判 `health_ok=false` | 端到端覆盖了此前只测到"前缀可读"的缝 |
| salvaged 段损失量化 | 已完成 | `segment_integrity()` 逐段报告压缩/解压字节、可读行数、丢弃的尾部半行字节、gzip footer 是否存在 | 被 SIGKILL 截断的流说不出丢了多少事件；本项只给出可读前缀的上界，不声称总损失 |
| attestation 读取来源 | 已完成 | `validate()` 与 `validate_historical()` 统一从 Git 对象取 sign-off/evidence/risk config | 保留一条窄回退：未提交文件读磁盘，供 `finalize_gate_b1` 一次性写入+生成的流程使用；HEAD 越过 freeze 后两文件仍必须出现在 committed diff |
| Windows NTFS disk-full（隔离 runner） | 已完成 | run 31562522619，192MB VHD 挂为 `R:`，真实 ENOSPC → fence RAISED → exit 10；ballast 180,879,360 bytes（低于 Linux 的 193,200,128，与 MFT 保留区一致） | 本平台第一次在真实 `ntfs.sys` 上直接观测 disk-full；owner 已接受托管 runner VHD 为生产等价卷（见 §3.1） |
| Windows publication 强杀 / ownership | 已完成 | 同一次运行 safe drill 4/4：durable replace 回读、两进程单一 owner、holder 强杀后 successor 取锁、publication 中途强杀后仍为完整 JSON generation | 同上 |
| Windows WAL damage/rollback + witness crossing | 已完成 | run 31574366903，同一隔离 VHD：真实 `ntfs.sys` 上 WAL recovery 静默丢弃 26 条已提交事件（3653→3627），全部在 witness `seq=1938` 之上故引擎正确启动；强制截到 witness 之下则 exit `15` + fence RAISED | 第一次在 Windows 内核文件系统上直接观测到"已 commit 的事务被无声丢弃、DB 仍自洽只是变短"；owner 已确认 §3.1 的接受覆盖该项（见 §3.2），清单条目已解除 |
| Windows flush / fsync stall | 已证伪可行性 | 云 Linux VM 内核无 device-mapper；FUSE 回退下 SQLite WAL 的 `-shm` mmap 直接使进程死于 signal 7，未走到超时判定 | Windows 无 dm-delay 等价物；托管 runner 上无法装过滤驱动。留 B3，不再尝试 |
| Windows Gateway 存活检测 | 已修复并实测 | CIM Access Denied 时继续用 `Get-Process`、`netstat` listener 和只读 API；现场返回 `RUNNING_API_VERIFIED`、server version 178 | 危险程序级动作仍额外要求 `path_status=MATCH`；查询不完整只能是 `INDETERMINATE` |
| Windows provenance 重新生成 | 已修复 | `STATE.json` 用 UTF-8 bytes + LF 写入；实测 `CR=0` 且 `provenance --check` 通过 | 不改变 B1 historical freeze，也不为当前 B2 worktree 提供新 attestation |
| 非空动态 reconciliation | 未完成 | 当前没有仓位、挂单或成交事实 | 需要另行授权 paper-order 子阶段 |
| `orderId / permId / clientId / orderRef` | 未完成 | 尚未产生真实订单身份事实 | 需要另行授权 paper-order 子阶段 |
| submit / ack / modify / cancel / fill / commission / late callback | 未完成 | 订单路径没有运行 | 不属于当前只读轮次 |

## 3. Owner 决定记录（2026-08-12）

以下两条是 owner 的明确判断，不是工程推导，也不由任何测试结果自动产生。记录在此以免后续被当作"尚未决定"重新翻出来。

### 3.1 托管 runner 上的隔离 VHD 计为生产等价卷 —— 已接受

[`OFFHOST_FAULT_DRILL_FEASIBILITY_20260812_ZH.md`](OFFHOST_FAULT_DRILL_FEASIBILITY_20260812_ZH.md)
把"托管 runner 的 192MB VHD 是否算生产等价 OS/volume"留为 owner 风险接受判断。**owner 于
2026-08-12 答：算。**

影响：`RECORDER_STORAGE_AND_WINDOWS_POLICY_ZH.md`「仍未解除的授权边界」清单中的 **NTFS disk-full**
与 **fence/witness publication 中途强杀** 两项，由 run 31562522619 的证据配合本次接受而解除。清单条目
按 amendment 方式标注，不删除原文。

未被本决定触及的：真实生产卷的几何与驱动栈仍与 VHD 不同；`order_authorization` 不受影响，仍为 `NONE`；
这不构成任何 Windows order-capable 授权。

### 3.2 §3.1 的接受覆盖 WAL/witness crossing —— 已确认

`RECORDER_STORAGE_AND_WINDOWS_POLICY_ZH.md` Amendment 1 当时明确写"本次接受只覆盖 NTFS disk-full 与
publication 强杀两项已实测的行为，不外推到清单其余各项"，因此 run 31574366903 取得的 WAL/witness
crossing 证据（见上表）未被自动划掉，留待 owner 单独确认。**owner 于 2026-08-12 答：覆盖。**

影响：`RECORDER_STORAGE_AND_WINDOWS_POLICY_ZH.md`「仍未解除的授权边界」清单中的 **journal WAL
damage/rollback 与 witness crossing** 一项由此解除，标注为 Amendment 2。清单剩余三项（写入/flush
stall、execution service 强杀、volume failure-domain 判定）不受影响。

### 3.3 1101 不再专门追 —— 已确认

45 秒 outbound block 只能产生 1100→1102。要触发 1101（requests lost，需重订阅）需要长得多的断开，
而收益仅是把一条已有实现和单元测试的分支从"未观察"变为"已观察"。**owner 于 2026-08-12 答：不追。**

影响：1101→重订阅在 [`DOCUMENTED_VS_OBSERVED.md`](DOCUMENTED_VS_OBSERVED.md) 中**永久保持"未直接观察"**，
代码路径保留，碰上真实 1101 时被动采集。不得由 1102 反推 1101，也不得因未观察而删除该分支。
任何未来的断网实验都不以取得 1101 为目的。

## 4. 只读实测的剩余边界（2026-08-12 复核）

原最高优先级“持三路订阅受控断网 + production `run()`”已经执行，详细见
[`GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md`](GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md)。本轮取得
1100→1102 而不是 1101：验证了 1102 不重订与恢复后三路增量，但不能关闭 1101→重订阅分支。
本轮还暴露旧实现按 0.25 秒 poll 重复写 380 条 marker；新 incident 生命周期已通过本地测试，但运行中
PID 18488 加载的是旧代码，因此必须把“真实旧问题”和“新代码离线修正”分开。

1. `completed orders` 被 Gateway Read-Only policy 阻断；继续需要降低保护，不接受。
2. overnight 与 RTH bounded 行情均已完成。2026-08-12 Full-RTH 尝试只取得约 17% 覆盖后失败；它验证了
   开盘至约 10:36 ET 的写盘前缀，但没有覆盖午盘、收盘 cross 或 `finalize_day` 的正常全日路径。必须先修复
   recovery 默认组合，再做短时真实 smoke，之后另日重跑。
3. 不应反复断网碰运气；持订阅实验也不得预设必得 1101。官方语义只支持按实际结果区分
   1101（requests lost，需要重订阅）与 1102（requests recovered），不得互相推断。
4. 非空 broker facts、订单身份、cross-client order visibility 和订单 callback 必须先获得 paper-order 独立授权。

现在仍可开展当前部分日运行的最终封存、官方文档复核、证据索引整理、代码审查和 freeze 准备。不得为
碰取 1101 重复断网；任何新的 fault injection 仍需 operator 对该次精确目标和窗口重新授权。

## 5. 已封存的本地证据索引

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
| 持订阅断网 + 部分日 production run | `artifacts/ib_preflight/20260812_controlled_disconnect_real_run_retry_empty_generic_ticks/raw/2026-08-11/health.json` | 已 finalize；partial-day health FAIL；`eba079cd70cdbedd7ebf49223eb5d19d9316ea47104b4014173dd962385534d5` |
| Full-RTH 失败轮 health | `artifacts/ib_preflight/20260812_full_rth_main_83e9573_v3/raw/2026-08-12/health.json` | `4ac241bebc39f5cabc8dee16024f3ce761ea32b1708f86d0b5e29fdf6a7146c9` |
| 事故后 120 秒 LIVE probe | `artifacts/ib_preflight/20260813_current_live_diagnosis_v1/report.json` | `f13956a327971319f0c8f200c1e84e2bea0dc0bb82e3f422a22fcd7e5e89cc9b` |

表中 2026-08-11 及以前的项目是已有本地 evidence bundle；网络中断目录的 `SHA256SUMS` 已复核。
2026-08-12/13 artifact 的状态按表内逐项标注；失败轮必须保留，但不属于 PASS 证据。
这里的“封存”只表示对应证据文件及 digest 已保存，**不表示 B2 已形成最终 Git exact-freeze**；新的
source、tests、docs 与后续真实 Gateway 证据仍需在最终 freeze 中统一绑定。

## 6. 后续计划与进入下一阶段的条件

1. SPY overnight 行情/Recorder 已完成；详细过程见 [`GATE_B2_OVERNIGHT_20260810.md`](GATE_B2_OVERNIGHT_20260810.md)。
2. 正式 RTH 三路行情与 bounded Recorder 已完成；详细过程见 [`GATE_B2_RTH_20260810.md`](GATE_B2_RTH_20260810.md)。
3. 保留并索引 2026-08-12 Full-RTH 失败轮；详细见
   [`INCIDENT_FULL_RTH_20260812_ZH.md`](INCIDENT_FULL_RTH_20260812_ZH.md)。先修复 scheduler 在生产默认
   `fast_attempts=2` + `evidence_of_life=True` 时错误 full reconnect 的分支，并补足回归测试。
4. 修复后做一次 **10–20 分钟只读冒烟跑**：确认 LIVE、三路持续、`halt_state_available=false`，且有生命
   迹象时不会 full reconnect。事故后的 120 秒 probe 已证明当前 entitlement/route 可用，但不能替代修复后 smoke。
5. 再做一次受控断网，跑在修复后的代码上，验证 incident 生命周期与恢复策略。
   任何 fault injection 仍需 operator 对该次精确目标和窗口重新授权。1101 保持“未观察”，不为取码反复断网
   （owner 决定，见 §3.3）。
6. P0 修复、回归和真实 smoke 均通过后，另一天从开盘前重跑 Full-RTH 全日 health，同时闭环
   `finalize_day`、全日 bar cadence 和长期资源行为。
   容量与队列水位按 soak 实测外推已基本不构成风险（7.19 gzip bytes/event × 约 562 万条 ≈ 40MB/天；
   soak 在 10,000 events/s 下 queue high-water 仅 2,329/100,000，而 RTH 实测峰值约 240 events/s），
   但仍应在开跑前用一个真实 RTH segment 复核每行字节数，不用 soak 的合成行代替。
7. 逐项完成官方 IB 文档复核，整理 B2 source、tests、docs 和 evidence，形成新的可复查 freeze；不得借用 B1 exact-freeze 为新代码背书。
8. 只读证据完成并封存后，由 owner **单独决定**是否授权“paper account、1 股 SPY、机械订单生命周期”的 paper-order protocol。只有进入该子阶段后，才验证非空 reconciliation、订单身份、跨 client 订单可见性、submit ambiguity、modify/cancel/fill、Gateway restart 和 late/duplicate/out-of-order callback；live order 继续禁止。

## 7. 文档导航

- 本文：当前状态、边界、证据索引和下一步。
- [`GATE_B2_OVERNIGHT_20260810.md`](GATE_B2_OVERNIGHT_20260810.md)：正确 overnight routing、三路行情及 bounded Recorder 写盘证据。
- [`GATE_B2_RTH_20260810.md`](GATE_B2_RTH_20260810.md)：RTH competing-session 失败、判定加固、正式三路行情及 bounded Recorder 写盘证据。
- [`GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md`](GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md)：持三路订阅的 production 断网、旧 marker 重复、incident 修正和 Gateway 检测修复。
- [`INCIDENT_FULL_RTH_20260812_ZH.md`](INCIDENT_FULL_RTH_20260812_ZH.md)：Full-RTH 提前终止、API log、恢复策略缺陷、owner 登录陈述和整改条件。
- [`GATE_B2_READONLY_20260809.md`](GATE_B2_READONLY_20260809.md)：每一轮真实 Gateway 测试的详细过程与观测。
- [`DOCUMENTED_VS_OBSERVED.md`](DOCUMENTED_VS_OBSERVED.md)：官方文档语义与真实 Gateway 直接观测的逐项矩阵。
- [`GATE_B1_SIGNOFF_117188cea539.md`](GATE_B1_SIGNOFF_117188cea539.md)：Gate B1 exact-freeze owner acceptance；其范围不包含真实 IB 行为。
