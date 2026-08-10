# Recorder 写入、测试与 Windows 部署边界

更新：2026-08-11。

本文回答三个容易被混在一起的问题：实际交易必须持久化什么、研究型 Recorder 如何避免阻塞行情 callback、以及 Windows 测试通过是否等于允许发单。

## 数据边界

订单执行 Journal 与行情 Recorder 是两个独立系统。

- 订单 Journal 必须在 broker write 前同步持久化 decision、intent、send/cancel、ACK、execution、reconciliation、HALT、fence 和 witness。该路径数据量低，不能为了吞吐改成“先返回成功、以后再落盘”。
- `execution_host` 不构造 `QuoteRecorder` 或 `RawEventLog`。实际交易默认不保存每一条 BidAsk tick；它只需要订单审计、决策输入快照、连接/数据流健康和 staleness。
- `QuoteRecorder` 是显式启动的只读研究进程，用于 full-session arrival spread、数据源比较和 tail-day 研究。只有该模式保存三路完整行情。

因此，全行情写入不是 paper/live order 的前置条件，也不应与订单 Journal 共用进程、Gateway pacing budget 或故障域。

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

manifest 保存 `write_accounting`：`accepted`、`persisted`、`dropped`、`queue_high_water`、`max_writer_lag_ms` 以及逐 stream 计数。bounded Gateway probe 另外比较 callback 侧 `handler_counts` 与磁盘 readback；任何不等都失败。

## 测试策略

健康数学测试使用内存 event source，不再为了验证 coverage、gap、clock skew 或 fatal-error 判定而反复写完整 6.5 小时 gzip。

真实存储测试仍覆盖：

- segment roll/rename/readback；
- same-day restart 与单 writer ownership；
- async append 不等待磁盘；
- queue overflow 和 writer failure；
- gzip → Parquet → readback → schema/hash/manifest；
- callback handler 与 persisted 计数。

2026-08-11 当前 Windows 环境中，`tests/test_recorder.py` 为 51 项全过，约 13.5 秒。完整 `pytest -q` 在不 deselect、不隐藏 Windows 分支的情况下运行到 100% 并返回 0。完整套件仍包含故意等待 timeout、子进程强杀和真实持久化边界的慢测试，所以总耗时不能用 Recorder 单文件预算衡量。

## Windows durable publication

`fatal_fence` 与 `journal_witness` 现在共用 `durable_atomic_write`：

- POSIX：temp file `fsync` → atomic replace → final file `fsync` → parent directory `fsync`；
- Windows：temp file `fsync` → `MoveFileExW(REPLACE_EXISTING | WRITE_THROUGH)` → final file handle `fsync`。

Windows 不再调用 `os.open(directory, O_RDONLY)`，也不会在 replace 已完成后因为该 POSIX 假设而误报失败。

`ProcessLock` 的控制与诊断也已分离：内核 byte-range lock 仍是唯一 ownership 控制；未加锁的 `.owner` sidecar 保存 PID、进程启动身份和说明，解决 Windows 上第二进程无法读取已锁文件的问题。PID/sidecar 从不代替内核锁，也不作为 stale-lock lease。

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
