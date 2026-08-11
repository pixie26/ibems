# Gate B2：持三路订阅的受控断网与真实 Recorder（2026-08-12）

状态：**部分完成；1100/1102 production 路径已直接观察，1101 未观察，Full-RTH 未完成。**

本轮使用 IB Gateway paper port 4002、`readonly=True`、SPY、`research_full` 和真实
`QuoteRecorder.run()`。没有发送、修改或取消订单，`order_authorization=NONE` 未改变。唯一故障动作是
在 owner 明确授权后，对 `D:\tws\ibgateway\ibgateway.exe` 创建一次 45 秒 outbound block；规则已清理并
再次确认不存在。本轮不能为 paper/live order 提供授权。

## 1. 两次启动必须分开记录

第一次运行保留在
`artifacts/ib_preflight/20260812_controlled_disconnect_real_run/`。Gateway 对 STK 的 generic tick 49
返回 error 321，Recorder 没有得到显式 LIVE market-data-type callback，按前置条件失败并正常封存：

- raw/Parquet/readback 共 5 条 SYSTEM；三路行情均为 0；
- `health_ok=false`，`market_data_type=UNKNOWN`；
- `writer_error=null`、`dropped_count=0`，失败在 IB 请求/前置条件，不在写路径；
- 该失败证据不得删除或改写成成功轮次。

第二次使用配置覆盖 `market_data_generic_ticks=""`，当时不改生产代码，artifact 为
`artifacts/ib_preflight/20260812_controlled_disconnect_real_run_retry_empty_generic_ticks/`。进程 PID 18488、
clientId 961，于约 13:00 ET 开始，当前仍为 `CAPTURING / READ_ONLY / research_full`。因为没有从 09:30 ET
开始，它无论是否运行到收盘都只是**部分日长跑**，绝不是 Full-RTH。

## 2. 受控断网的直接观测

从已关闭、不可变的 gzip segments 复核：

| 观测 | 结果 |
|---|---|
| 首个 1100 | `2026-08-11T17:03:38.544596Z` |
| 1102 | `2026-08-11T17:05:19.916811Z` |
| 1101 | 0 次；不得由 1102 推断 |
| connection epoch | 始终为 1 |
| `RESUBSCRIBE_REQUIRED` | 0 次，符合 1102 不重订阅 |
| 1102 后首个 BidAsk 本地接收 | +0.264 秒 |
| 1102 后首个 AllLast 本地接收 | +0.267 秒 |
| 1102 后首个 BAR_5S 本地接收 | +0.896 秒 |

断网期间还直接收到 2103/2105/2157 等 farm-down 状态。45 秒 block 清理后，IB 到 1102 的恢复存在额外
延迟；从首个 GAP marker 到 1102 约 101.8 秒。因此“防火墙阻断时长”和“数据覆盖缺口时长”必须分别记录。

这次实验直接验证了：production `run()` 在已建立三路订阅时能够记录 1100、保持进程存活、不在 1102
路径主动重订，并在恢复后重新收到 BidAsk、AllLast 和 5 秒 bar。它没有进入 1101，因此 1101→重订阅仍是
**未验证真实分支**。

## 3. 本轮暴露并修正的代码问题

### 3.1 轮询频率泄漏进审计日志

旧进程每 0.25 秒对同一 WAIT 状态写一条 `GAP_SUSPECTED`，本轮关闭 segments 中共有 380 条。这不是
380 个 incident，而是一个持续 outage 被采样约 380 次。写入量相对行情不大，但会污染审计语义。

新代码改为生命周期记录：

- `FEED_OUTAGE_START/UPDATE/CHECKPOINT/END`：1100/farm-down 等真实 feed outage；
- `EXPECTED_SILENCE_*`：halt/calendar 等合法静默；
- `GAP_SUSPECTED_*`：没有 IB 解释的 bar heartbeat 丢失；
- 相同状态不随 0.25 秒 poll 重复；默认每 60 秒最多一个 durable checkpoint；
- reconnect、1102 或重发订阅本身不足以写 END；必须看到 incident 开始后的首个真实 BAR_5S；
- crash 留下 START 无 END，并在 manifest 中显式记录 `open_incident_count=1`。

100 秒 outage 的单元测试由约 400 条降为 START、CHECKPOINT、END 三条；实质状态变化可额外产生 UPDATE。
但当前 PID 18488 在代码修改前已加载旧实现，所以这次真实 artifact 只能证明旧问题存在，**不能证明新
incident 实现在真实 Gateway 上已通过**。下一次 Recorder 启动才会加载修正。

### 3.1b generic tick 49 的默认值本身就是缺陷

当时只用配置覆盖绕过，生产默认仍是 `market_data_generic_ticks="49"`，也没有任何地方处理 error 321
——**下一次用默认配置启动的 `run()` 会以完全相同的方式失败**。引入该默认值时的论证是"显式请求严格占优：
IB 本来就发就无害，不发就必需"。这个论证被真实 Gateway 证伪了：请求一个不被支持的 generic tick 不是
免费的，它会连带废掉整个 `reqMktData`。

已修正：默认回到 `""`；operator 可在支持 tick 49 的环境显式 opt-in；两个方向都有回归测试。

更重要的是随之而来的**设计后果**：停牌抑制器现在没有输入源。没有 tick 49，真实停牌在本探测器眼中
与订阅死亡完全相同。因此 manifest 新增 `halt_state_available` / `halt_state_note`，明确记录抑制器当时
是否接通；读者不得把"没有 halt marker"读成"没有停牌"。

由此还暴露一个尚未决定的策略问题：停牌期间若 bar 停止且无 tick 49 解释，当前路径是
`RECOVER_SUBSCRIPTION` → 重连 → 仍无 bar → 再重连 →**耗尽 `ReconnectBudget` 后整个 session 终止**。
对只读 Recorder 而言，"整天数据没了"比"一段被标注的缺口"更糟；fail-closed 的直觉适用于订单路径，
不一定适用于数据采集。是否应在有限次恢复失败后降级为"保持 incident 打开并继续记录"，需 owner 决定，
本文不擅自改变。

### 3.2 非致命 ib_async callback 异常

恢复期间 console 出现一次 `ib_async.wrapper.contractDetails` 的 `KeyError: 2`。Recorder 没退出，三路随后
恢复。该异常与 gap incident 分开保留为 library/callback warning；它不是 380 条 marker 的原因，也不能因
进程继续运行就删除。当前只直接证明“本轮非致命”，尚未证明所有同类 KeyError 都可忽略。

### 3.3 Gateway 存活检测误判

旧的临时检查在 `Get-CimInstance Win32_Process` 返回 Access Denied 后过早退出，把“路径元数据不可读”
误报成“Gateway 未开启”。实际 Gateway 一直是 PID 19060、路径
`D:\tws\ibgateway\ibgateway.exe`、监听 `0.0.0.0:4002`。

现已新增正式分层检测：进程、路径、listener（`Get-NetTCPConnection` 不可用时回退 `netstat -ano`）和真实
只读 IB API handshake 分别记录。现场再次复现 CIM Access Denied，同时得到 PID 19060、4002 listener、
`path_status=MATCH`、API server version 178，最终为 `RUNNING_API_VERIFIED`。只有进程与 listener 查询均
成功且为空、API 也未成功时才允许 `NOT_RUNNING`；查询不完整必须是 `INDETERMINATE`。程序级防火墙动作
额外要求 `path_status=MATCH`，路径未知时即使 Gateway 正在运行也 fail closed。

### 3.4 Windows provenance 行尾

`.gitattributes` 的 `* -text` 固定了 checkout 字节，但 `Path.write_text()` 仍会在 Windows 重新生成 CRLF。
生成器现改为显式 UTF-8 bytes + LF，并有回归测试；`STATE.json` 实测 `CR=0`，provenance `--check` 通过。

## 4. 当前结论与剩余项

1. 持三路订阅的受控断网 + 真实 `run()`：**部分完成**。1100→1102、三路恢复和“不重订”已观察；1101
   未观察。
2. 新 incident 生命周期：**代码与测试通过，真实 Gateway 尚未复测**。
3. 当前长跑：**部分日进行中**，尚无最终 manifest/health；不能提前写 PASS。
4. Full-RTH：仍需另一天从开盘前启动，覆盖开盘、午盘、收盘及 `finalize_day`。
5. 1 股 SPY paper order：仍未授权，必须放在只读证据与新 freeze 之后；live order 继续禁止。

当前 artifact 尚在写入，不提供最终 digest。进程自然结束并完成 `finalize_day` 后，必须再核对
handler→selected→enqueued→persisted→readback、dropped/writer_error、health、manifest 和文件 SHA；在此之前
本文件只记录已经直接观察且不会因尾段增长而改变的事实。
