# Recorder 写入、测试与 Windows 部署边界

更新：2026-08-12。

本文回答三个容易被混在一起的问题：实际交易必须持久化什么、研究型 Recorder 如何避免阻塞行情 callback、以及 Windows 测试通过是否等于允许发单。

## 数据边界

订单执行 Journal 与行情 Recorder 是两个独立系统。

- 订单 Journal 必须在 broker write 前同步持久化 decision、intent、send/cancel、ACK、execution、reconciliation、HALT、fence 和 witness。该路径数据量低，不能为了吞吐改成“先返回成功、以后再落盘”。
- `execution_host` 不构造 `QuoteRecorder` 或 `RawEventLog`。实际交易默认不保存每一条 BidAsk tick；它只需要订单审计、决策输入快照、连接/数据流健康和 staleness。
- `QuoteRecorder` 是显式启动的只读研究进程，用于 full-session arrival spread、数据源比较和 tail-day 研究。只有该模式保存三路完整行情。

因此，全行情写入不是 paper/live order 的前置条件，也不应与订单 Journal 共用进程、Gateway pacing budget 或故障域。

代码现在显式定义三种模式，manifest 不再要求读者从文件名猜测数据是否完整：

- `execution_minimal`：禁止启动 `RawEventLog`。执行主机只保存同步订单审计、TARGET 接收时的 bid/ask/last 快照、连接/行情状态、fence 和 witness。
- `evidence_sampled`：完整保存 AllLast 和 5 秒 bar；BidAsk 在固定间隔、价格变化或 decision window 任一条件满足时保存。`mark_decision()` 会先提升内存 ring 中的完整 pre-decision BidAsk，再打开完整 post-decision window；前后窗口、采样间隔和价格变化规则全部写入 manifest。
- `research_full`：三路行情全部保存，用于滑点、arrival spread 和数据源研究。

`QuoteRecorder --mode` 只接受 `evidence_sampled` 或 `research_full`；选择 `execution_minimal` 会拒绝构造 Recorder，防止“最小执行模式”意外变成全行情落盘。

## Recorder 写入实现

IB callback 不再执行 JSON 编码、gzip flush 或 `fsync`。`RawEventLog.append()` 只构造队列项并执行非阻塞 `put_nowait`；独立 writer 线程负责：

1. 最多每 512 条或 50ms 组成一个 batch；
2. 批量 JSON 编码并写入 gzip segment；
3. 每个 batch flush Python/gzip buffer；
4. 默认每 1 秒 `fsync`，roll/close 时强制完成 gzip footer 与最终 `fsync`；
5. 关闭时排空队列并发布最终 segment。

队列默认上限 100,000 条，可通过 `--queue-capacity` 调整。它是有界缓冲，不是把整日数据无限留在内存。

以下情况全部 fail-closed，不允许生成健康 PASS：

- 队列满；
- writer 线程异常退出；
- flush/close 超时；
- accepted、persisted 或 per-stream 计数不一致；
- callback handler 异常。

manifest 保存完整链路：`handled → selected → enqueued → persisted → readback`、`filtered`、`dropped`、逐 stream/逐 run 计数、`queue_high_water`、`max_writer_lag_ms` 和 `fsync_latency_ms`。任何不等都失败。`research_full` 必须 `filtered=0`；`evidence_sampled` 必须满足 `handled=selected+filtered`，且 manifest 必须携带采样规则。

## Event-loop watchdog

Recorder 现在有独立 heartbeat publisher 线程，但该线程不会自行刷新 event-loop 时间。只有 IB event loop 调用 `pulse()` 才会推进 `heartbeat_mono`；因此 `reqCurrentTime` 或其他 IB 请求卡住时，publisher 虽仍能写状态文件，外部 watchdog 看到的 pulse 仍会持续变旧。

生产/验证运行应把 `scripts/run_recorder_watchdog.py` 作为独立进程启动。默认建议 heartbeat timeout 15 秒、再等待 15 秒 grace；watchdog 只告警和终止 Recorder，不下单、不重启。

## 行情 liveness：只有 bar 有资格中断运行

原先三路行情各有一个运行时阈值，任何一路超时都记 `STREAM_STALE` 并重建订阅。这个设计有一个无法通过调参解决的缺陷：对**事件驱动**的流，"5 秒没有 BidAsk" 和 "5 秒内没有报价变化" 是同一个观测，两者在信息上不可区分。结果是一个正常清淡的夜盘会产生和"订阅已死"完全相同的报错 —— 而后者正是这套机制存在的理由。

真实行情协议（ITCH / OPRA / CME MDP 3.0）不靠计时解决这件事，靠的是每通道单调序列号：收到 1001 再收到 1003 就**确定**丢了 1002，不需要阈值。IB 不提供序列号，但提供了另一半 —— `reqRealTimeBars` 是**时间驱动**的。

2026-08-10 的 OVERNIGHT 实测（[`GATE_B2_OVERNIGHT_20260810.md`](GATE_B2_OVERNIGHT_20260810.md) §3，120.047 秒窗口）直接观察到：AllLast 只有 13 笔、最大间隔 29.641 秒，同一窗口 5 秒 bar 有 25 根、最大间隔 5.235 秒。成交几乎停摆，bar 节奏纹丝不动。即使 `whatToShow="TRADES"`，零成交的 5 秒窗口 IB 照样发 bar。

因此现在的划分是：

| 流 | 性质 | 权限 |
|---|---|---|
| `BAR_5S` | 时间驱动 | heartbeat，可中断运行（默认 12 秒 = 2 个周期 + 余量） |
| `BID_ASK` / `ALL_LAST` | 事件驱动 | 只记录进报告，**永远不中断运行** |

"这段静默要不要报警"由 IB 的显式信号回答，不由时长推断：generic tick 49（0=正常，1=停牌，2=波动性熔断）、market data farm 状态码 2103/2105 与 2104/2106、以及 1100/1101/1102 连接三元组。1101 表示订阅已丢必须重订，1102 表示订阅保留、重订反而会自己制造一段缺口。2108 是 IB 明说的"非错误"，不当作故障 —— 把它当故障是让 operator 学会无视告警的最快方式。另外 `ib.setTimeout()` / `timeoutEvent` 监听"完全没有任何数据从 TWS 过来"，它跑在 event loop 上，只能发现对端沉默；本地 loop 卡死由独立线程的 `EventLoopHeartbeat` 负责，两个失败域两个探测器。

真实 Gateway 在 2026-08-12 对 STK 的 generic tick 49 直接返回 error 321，整个 `reqMktData` 因此拿不到 LIVE 回调，三路全部归零。**默认已改回不请求 tick 49**（`market_data_generic_ticks=""`），需要停牌态的环境由 operator 显式传入 `"49"` 才会请求。这个默认值反过来意味着停牌抑制器在多数环境下没有输入：`MarketLiveness.manifest()` 现在带 `halt_state_available` / `halt_state_note`，读者不得把"没有 halt marker"读成"没有停牌"——它更可能是"这套环境根本拿不到这个信号"。

任何非 CONTINUE 判定都必须**先**留下 raw-log 证据再决定动作，但不能把 0.25 秒 polling cadence
伪装成 incident count。2026-08-12 的真实断网旧进程为一个持续 outage 写了 380 条
`GAP_SUSPECTED`；新实现改为事件生命周期：IB 明示 1100/farm-down 使用
`FEED_OUTAGE_START/UPDATE/CHECKPOINT/END`，halt/calendar 使用 `EXPECTED_SILENCE_*`，没有解释的 bar
丢失才使用 `GAP_SUSPECTED_*`。相同状态不重复，默认每 60 秒最多一个 checkpoint；reconnect、1102 或
重发订阅本身不能闭合 coverage incident，只有 incident 开始后的首个真实 BAR_5S 才能写 END。进程在
恢复前崩溃会留下 START 无 END，manifest 明示 open incident，而不是制造恢复。

### 修复动作：先修最小的坏东西，永不因静默退出

没有 tick 49 时，一次停牌会让 bar 停、又拿不到任何解释——旧实现把这类"未解释的心跳丢失"直接交给
`ReconnectBudget`（15 分钟 5 次、全天 20 次的爆发闸），一次 5 分钟的停牌就足以在一两分钟内耗尽爆发闸，
提前终止整个 session，剩下的交易时段一条数据都录不到。这是 owner 的明确决定：**对一个唯一产出是数据
的进程，"整天没数据"比"一段被标注的缺口"更差；只读 Recorder 上不应套用下单路径"宁可什么都不做"的
fail-closed 直觉。**

修复动作由 `RecoveryScheduler` 决定，两条规则：

- **修最小的坏东西**：如果 bar 停了但 BidAsk/AllLast 仍在到达，说明 socket 和两路 tick-by-tick 订阅
  都还活着——为了修一路死掉的 bar 去重连整个连接，等于用锤子砸掉两路还好着的数据。此时只
  `cancelRealTimeBars` + 重新 `reqRealTimeBars`，不碰 socket，代价接近零，可以每 120 秒试一次。只有
  三路全静、确实没有东西可保护时，才做完整重连。
- **重试频率取决于重试的代价，不取决于问题的严重性**：三路全静时完整重连从 5 分钟起步，每次失败翻倍到
  30 分钟封顶——第一百次失败既不会更可能修好，也不值得再写一条一样的审计记录。IB 的任何一条入站消息
  都会把下次尝试拉到最多一个基础间隔之后，但不会取消当前等待，避免断网期间对端一多话，退避就被打成锤子。

这类修复调用**故意不计入** `ReconnectBudget`：那个预算是用来防止对着一个真实错误反复抖动，而这条路径已
经由自己的退避节流，停止条件是收盘，不是次数上限。它们单独计数，写进 `liveness.recovery` manifest。

**配套的诚实性约束**：既然不再退出，`health_ok` 就必须能如实反映丢失。`FEED_OUTAGE`、`GAP_SUSPECTED`
以及任何在收盘时仍未闭合的 incident，现在都会让当天 `health_ok=false`，并把次数、总时长、未闭合 incident
的 id 和已持续时长写进 `problems`。单纯的 `EXPECTED_SILENCE`（停牌、日历静默）不会让当天失败——那样会
训练读者无视 `health_ok`，连真实故障一起无视掉。

持三路订阅的真实 production fault 已观察 1100→1102、不重订和三路恢复，证明 bar heartbeat 路径确实
进入真实 `run()`；但未观察 1101，新 incident 生命周期和上面这套修复策略又都是在该运行启动后加入的，
不能倒推为真实 PASS。仍需 Full-RTH 覆盖开盘/午盘/收盘 5 秒节奏，并解释 `useRTH=True` 在 OVERNIGHT
route 下的行为。**未测就当作 pass 条件，等于重犯它所替换的错误。**

## 测试策略

健康数学测试使用内存 event source，不再为了验证 coverage、gap、clock skew 或 fatal-error 判定而反复写完整 6.5 小时 gzip。

真实存储测试仍覆盖：

- segment roll/rename/readback；
- same-day restart 与单 writer ownership；
- async append 不等待磁盘；
- queue overflow 和 writer failure；
- gzip → Parquet → readback → schema/hash/manifest；
- callback handler 与 persisted 计数。

另外覆盖 60 秒虚拟 session 的开收盘/gap 边界、writer drain timeout 不释放仍在工作的 session lock、Recorder 进程强杀后的 gzip prefix salvage，以及 event-loop pulse 停止时外部 watchdog 判定 stale。现有强杀测试证明可读前缀能够恢复并保留 `crashed-*` 段。**该段现在必须被披露**：`compute_health` 收集所有 `crashed-*` 段名进 `salvaged_segments`，写入 `health.json`，并产生一条 `capture truncated: ...` problem，使 `health_ok=false`。此前唯一痕迹是 `file_hashes` 里的一个文件名，读者必须自己注意到，而 `health_ok` 仍为 true —— 一个丢了尾巴的交易日和一个完整交易日在 manifest 里长得一样。salvage 出来的行是真的，值得恢复；但该段在内核停下 writer 的地方结束，任何从它得到的计数都不完整，因此不能报成干净的一天。仍待补的是**段级**（而非整段丢弃）完整性判定：明示尾部被丢弃了多少字节。

重型吞吐验证已移到 `.github/workflows/recorder-soak.yml`：Windows/Linux 每周或手动写入并 readback 一百万事件，输出吞吐、字节数、队列水位、writer lag、fsync latency 和零丢失对账。普通 PR CI 不重复写一整天数据。

2026-08-11 本机 million-event soak 已在目标 10,000 events/s 下通过：1,000,000 accepted/persisted/readback、dropped=0，100 秒完成，gzip 共 7,190,604 bytes，queue high-water 2,329，max writer lag 234ms，98 次 fsync 的 p95/max 为 63/125ms。按“实测峰值 10,000 events/s × 可容忍磁盘停顿 10 秒”得到推荐 queue capacity 100,000，与默认值一致；未来峰值或停顿预算变化必须重跑校准。

## Windows durable publication

`fatal_fence` 与 `journal_witness` 现在共用 `durable_atomic_write`：

- POSIX：temp file `fsync` → atomic replace → final file `fsync` → parent directory `fsync`；
- Windows：temp file `fsync` → `MoveFileExW(REPLACE_EXISTING | WRITE_THROUGH)` → final file handle `fsync`。

Windows 不再调用 `os.open(directory, O_RDONLY)`，也不会在 replace 已完成后因为该 POSIX 假设而误报失败。

`ProcessLock` 的控制与诊断也已分离：内核 byte-range lock 仍是唯一 ownership 控制；未加锁的 `.owner` sidecar 保存 PID、进程启动身份和说明，解决 Windows 上第二进程无法读取已锁文件的问题。PID/sidecar 从不代替内核锁，也不作为 stale-lock lease。

本机真实 NTFS safe drill 已直接通过：两进程只能一个持锁、holder 强杀后 successor 可取得锁、连续 durable replace 可读、publication writer 中途强杀后目标仍是完整 JSON generation。证据由 `scripts/run_windows_ntfs_safe_drill.py` 生成。

真实 disk-full 已提供隔离 VHD runner：`scripts/run_windows_ntfs_vhd_disk_full.ps1` 只在 `artifacts/` 新建 128–512MB VHD、格式化该临时盘、运行 execution-host ENOSPC drill，最后卸载删除；`.github/workflows/windows-ntfs-fault.yml` 可在独立 Windows runner 上封存结果。本机尝试因当前会话没有可用 Windows 磁盘管理提权而未创建 VHD，主工作盘没有被写满。该实验不阻塞 B2：Recorder 已通过 `dropped_count` / `writer_error` 暴露失败，当前后果是研究数据不完整而非无审计继续下单。VHD workflow 保留为手动、隔离 runner 项，绝不在主盘执行；有真实 order Journal 路径后，应以 Journal fail-closed 为主要被测对象。

修订 2026-08-12：`run_windows_ntfs_vhd_disk_full.ps1` 原先硬编码 `.venv312\python.exe`，而 workflow 用 `uv sync` 生成的是 `.venv\Scripts\python.exe`；该行在 runner 上必然失败，且失败点在 VHD 已创建挂载之后。现改为可传入 `-PythonExe`、否则按候选顺序探测，并把解析移到任何磁盘操作之前。哪些故障演练能在非 owner 本机的环境完成、各自能证明到什么程度，见 [`OFFHOST_FAULT_DRILL_FEASIBILITY_20260812_ZH.md`](OFFHOST_FAULT_DRILL_FEASIBILITY_20260812_ZH.md)——其中云 Linux VM 上的 NTFS 结果走的是 ntfs-3g/FUSE，只复刻磁盘格式而非 `ntfs.sys` 语义，不注销本节的 Windows 欠账。修复后经 owner 批准在隔离 `windows-2025` runner 上实跑（run 31562522619，`45c20e5`），safe drill 4/4 PASS 且 VHD disk-full 以 exit 10 + fence RAISED 通过——这是本平台第一次在真实 Windows NTFS 驱动上直接观测到 disk-full 行为，此前该段记载的"本机未创建 VHD"仍然属实，两者不冲突。

## 仍未解除的授权边界

Windows 单元、子进程和完整套件通过，只证明当前 API 调用形状和互斥行为可用；它不等于真实 NTFS 故障语义已经闭环。以下项目是 B3/order-capable Windows deployment 的前置证据，不是 B2 只读 Gateway/Recorder 的 blocker：

任何 order-capable Windows deployment 前仍必须在生产等价 OS/volume 上完成并封存：

- ~~NTFS disk-full~~ —— **已解除**（见下方 amendment 1）。原文保留：已在隔离 `windows-2025` runner 上直接观测通过（run 31562522619，192MB VHD 挂为 `R:`，真实 ENOSPC → fence RAISED → exit 10）；
- 写入/flush stall；
- ~~fence/witness publication 中途强杀~~ —— **已解除**（见下方 amendment 1）。原文保留：同一次运行在真实 NTFS 上通过（publication writer 中途强杀后目标仍是完整 JSON generation，两进程单一 owner，holder 强杀后 successor 取锁）；
- journal WAL damage/rollback 与 witness crossing；
- execution service 强杀、ownership 继承与 startup refusal；
- volume failure-domain 判定。

### Amendment 1（2026-08-12）：owner 接受托管 runner VHD 为生产等价卷

上面两条此前不能由 run 31562522619 自动划掉，因为"托管 runner 上的 192MB VHD 是否算生产等价
OS/volume"是风险接受判断而不是测量结果。**owner 于 2026-08-12 答：算。**因此 NTFS disk-full 与
fence/witness publication 中途强杀两项解除，证据即该次运行。

条目按 amendment 划线保留而不删除，理由是这份清单本身是 order-capable 部署的前置证据台账：把已解除
的条目连同解除依据一起留在原位，比让台账显得"从来只有四项"更可复查。

本 amendment 的边界：真实生产卷的几何与驱动栈仍与 VHD 不同，本次接受只覆盖上述两项已实测的行为，
不外推到清单其余各项；`order_authorization` 不受影响，仍为 `NONE`；这不构成任何 Windows
order-capable 授权。判断记录见
[`GATE_B2_STATUS_20260810_ZH.md`](GATE_B2_STATUS_20260810_ZH.md) §3.1。

剩余四项中，**journal WAL damage/rollback 与 witness crossing** 是唯一还能低成本推进的一项：
`run_storage_fault_drill.py --drill wal_corruption` 是纯 Python，同一个隔离 VHD 卷即可承载，只是
`run_windows_ntfs_vhd_disk_full.ps1` 目前只传 `--drill disk_full`。flush stall 已证实在托管 runner 上无解
（Windows 无 dm-delay 等价物），service 强杀与 volume failure-domain 尚无脚本。

其中 flush stall、fence/witness publication、journal WAL damage/rollback、service 强杀和 volume failure-domain 都属于订单持久化故障域；相关 order Journal 代码与授权未进入真实 broker 路径前，不在 B2 重复做高风险本机实验。B2 当前只保留低风险的 Recorder gzip 尾段完整性小项。

在这些 B3/order-capable 证据完成前，Windows 只授权 read-only Recorder/Gateway validation；`order_authorization` 仍为 `NONE`。若生产执行放在已有 Linux 故障证据覆盖的环境，Windows 可继续作为开发和只读观测机，但 Linux freeze 也不能自动为新的 B2 代码背书。

该限制现在不仅是文档：`HostConfig.broker_capability=order_capable` 时，启动前必须提供平台匹配的 exact-freeze capability evidence，包含 owner `PAPER/LIVE` 授权、source tree hash、全部必需 fault drill PASS 和 artifact SHA-256；缺任一项都在 broker 构造前拒绝启动。默认 capability 是 `simulation`，当前没有任何通过文件，也不会因测试绿色自动产生授权。
