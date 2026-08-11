# Recorder 写入、测试与 Windows 部署边界

更新：2026-08-11。

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

生产/验证运行应把 `scripts/run_recorder_watchdog.py` 作为独立进程启动。默认建议 heartbeat timeout 15 秒、再等待 15 秒 grace；watchdog 只告警和终止 Recorder，不下单、不重启。每路行情也有独立运行时 staleness：socket 仍连接但 BidAsk、AllLast 或 BAR_5S 超过各自阈值时，Recorder 会记录 `STREAM_STALE` 并重建订阅，不能静默完成。

## 测试策略

健康数学测试使用内存 event source，不再为了验证 coverage、gap、clock skew 或 fatal-error 判定而反复写完整 6.5 小时 gzip。

真实存储测试仍覆盖：

- segment roll/rename/readback；
- same-day restart 与单 writer ownership；
- async append 不等待磁盘；
- queue overflow 和 writer failure；
- gzip → Parquet → readback → schema/hash/manifest；
- callback handler 与 persisted 计数。

另外覆盖 60 秒虚拟 session 的开收盘/gap 边界、writer drain timeout 不释放仍在工作的 session lock、Recorder 进程强杀后的 gzip prefix 恢复，以及 event-loop pulse 停止时外部 watchdog 判定 stale。

重型吞吐验证已移到 `.github/workflows/recorder-soak.yml`：Windows/Linux 每周或手动写入并 readback 一百万事件，输出吞吐、字节数、队列水位、writer lag、fsync latency 和零丢失对账。普通 PR CI 不重复写一整天数据。

2026-08-11 本机 million-event soak 已在目标 10,000 events/s 下通过：1,000,000 accepted/persisted/readback、dropped=0，100 秒完成，gzip 共 7,190,604 bytes，queue high-water 2,329，max writer lag 234ms，98 次 fsync 的 p95/max 为 63/125ms。按“实测峰值 10,000 events/s × 可容忍磁盘停顿 10 秒”得到推荐 queue capacity 100,000，与默认值一致；未来峰值或停顿预算变化必须重跑校准。

## Windows durable publication

`fatal_fence` 与 `journal_witness` 现在共用 `durable_atomic_write`：

- POSIX：temp file `fsync` → atomic replace → final file `fsync` → parent directory `fsync`；
- Windows：temp file `fsync` → `MoveFileExW(REPLACE_EXISTING | WRITE_THROUGH)` → final file handle `fsync`。

Windows 不再调用 `os.open(directory, O_RDONLY)`，也不会在 replace 已完成后因为该 POSIX 假设而误报失败。

`ProcessLock` 的控制与诊断也已分离：内核 byte-range lock 仍是唯一 ownership 控制；未加锁的 `.owner` sidecar 保存 PID、进程启动身份和说明，解决 Windows 上第二进程无法读取已锁文件的问题。PID/sidecar 从不代替内核锁，也不作为 stale-lock lease。

本机真实 NTFS safe drill 已直接通过：两进程只能一个持锁、holder 强杀后 successor 可取得锁、连续 durable replace 可读、publication writer 中途强杀后目标仍是完整 JSON generation。证据由 `scripts/run_windows_ntfs_safe_drill.py` 生成。

真实 disk-full 已提供隔离 VHD runner：`scripts/run_windows_ntfs_vhd_disk_full.ps1` 只在 `artifacts/` 新建 128–512MB VHD、格式化该临时盘、运行 execution-host ENOSPC drill，最后卸载删除；`.github/workflows/windows-ntfs-fault.yml` 可在独立 Windows runner 上封存结果。本机尝试因当前会话没有可用 Windows 磁盘管理提权而未创建 VHD，主工作盘没有被写满。

## 仍未解除的授权边界

Windows 单元、子进程和完整套件通过，只证明当前 API 调用形状和互斥行为可用；它不等于真实 NTFS 故障语义已经闭环。

任何 order-capable Windows deployment 前仍必须在生产等价 OS/volume 上完成并封存：

- NTFS disk-full；
- 写入/flush stall；
- fence/witness publication 中途强杀；
- journal WAL damage/rollback 与 witness crossing；
- execution service 强杀、ownership 继承与 startup refusal；
- volume failure-domain 判定。

在这些证据完成前，Windows 只授权 read-only Recorder/Gateway validation；`order_authorization` 仍为 `NONE`。若生产执行放在已有 Linux 故障证据覆盖的环境，Windows 可继续作为开发和只读观测机，但 Linux freeze 也不能自动为新的 B2 代码背书。

该限制现在不仅是文档：`HostConfig.broker_capability=order_capable` 时，启动前必须提供平台匹配的 exact-freeze capability evidence，包含 owner `PAPER/LIVE` 授权、source tree hash、全部必需 fault drill PASS 和 artifact SHA-256；缺任一项都在 broker 构造前拒绝启动。默认 capability 是 `simulation`，当前没有任何通过文件，也不会因测试绿色自动产生授权。
