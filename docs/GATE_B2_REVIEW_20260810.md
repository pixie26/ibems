# Gate B2 只读轮次复核（2026-08-10）

对 `main` 上 `964c3a7 → ef8ec7e` 三个 B2 提交、五个新 probe 脚本和四份证据文档的独立复核。

结论：**这一轮的 Gateway 实测质量总体良好，但测量装置仍有边界**，尤其 RTH v1 的处理方式——先暴露 `10197`、承认旧判定会写出 `passed=true`、补机械检查、再用独立 v2 作为证据——是正确方向。2026-08-11 的二次复核同意第 1、2、4 条核心判断；第 3、5 条保留为高价值假设，但修正了原文中过强的因果和确定性表述。

## 1. 已确认缺陷：production 代码里每个 IB 请求都可以永久挂起

`ib_async` 的 `IB.RequestTimeout` 默认值是 `0`，而 `IB._run` 把它直接交给
`util.run(*awaitables, timeout=self.RequestTimeout)`——`0` 的语义是**永不超时**。
`reqCurrentTime()` 只是 `self._run(self.reqCurrentTimeAsync())`，而
`reqCurrentTimeAsync` 返回的 future 只能由 broker 的 `currentTime` callback 完成。

五个 probe 脚本**全部**自己设了 `ib.RequestTimeout`：

```text
scripts/run_ib_readonly_preflight.py:284
scripts/run_ib_readonly_overnight_recorder_probe.py:64
scripts/run_ib_readonly_client_fault_probe.py:81,146
scripts/run_ib_readonly_gateway_restart_probe.py:83
scripts/run_ib_readonly_network_fault_probe.py:96
```

`src/ib_execution/quote_recorder.py` 在这次修复之前**一次都没有设**。

所以 `DOCUMENTED_VS_OBSERVED.md` 里「请求无 completion 有硬 deadline」这一条，
描述的是 **probe harness 的属性，不是被测代码的属性**。probe 之所以从来没挂住，
是因为 probe 自己带了 deadline；`QuoteRecorder.run()` 在 line 1124 和 1183 调用
`measure_clock_skew(ib)`，后者对真实 Gateway 做 `reqCurrentTime`——而这一轮
**直接观测到**真实 Gateway 在 0.2 秒间距下漏掉过这个 callback。

1.1 秒 pacing 降低触发率，但没有给等待加上界。这个失败形状是最坏的一种：被阻塞的
是**唯一的那个 event loop**，所以 recorder 同时停止读行情、停止跑自己的健康检查、
`isConnected()` 仍为 True、没有任何异常——**没有任何还在运行的东西能发现它**。
这与不变量 18（callback/bridge 失败必须 fail-closed）是同一族问题，而且未来
`IbAdapter` 会沿用同一个调用形状。

已修：`enforce_request_deadline()` + `run()` 内绑定 + 结构测试。`request_deadline_seconds`
进入 `RecorderConfig`，`<= 0` 拒绝。该 deadline 约束 `ib_async` 的 blocking request；
subscription/cancel 这类立即返回的调用不是同一种 completion wait。

## 2. 已确认缺陷：行情 callback 里的异常会被吞掉，run 仍报成功

`_wire_ticker` / `_wire_bars` 的 handler 跑在 `eventkit` 的 dispatch 里。
`eventkit/event.py:107-116`：

```python
except Exception as error:
    if caller.error_event:
        caller.error_event.emit(caller, error)
    else:
        logger.exception("Value %s caused exception for event %s", args, caller)
```

**捕获、记录、继续。** 仓库里没有任何地方接 `error_event`，probe 也没装会失败的
logging handler。而 `on_update` 是在一个 `for tick in updated.tickByTicks` 循环里写盘的
——所以任意一条 tick 抛异常，会**丢掉这次 TCP update 剩下的整个 tick buffer**，
run 继续跑完，report 写 `no_exception: true`、`passed: true`。

即：**RTH / OVERNIGHT recorder 报告里的「无异常」不构成「没有丢数据」的证据。**
这和当初 `AllLast=0` 的问题是同一类——仪表给出的数字不能承载读它的人赋予的含义。
而且后果更重：Recorder 是回测的 data of record，一个自称完整的短日志比一次失败的 run 更坏。

候选修复先把 handler 异常写入 `_fatal_prerequisite_error`，并给 bounded probe 新增
`no_swallowed_callback_failure`。二次复核发现 **production `QuoteRecorder.run()` 主采样循环
原本没有轮询该字段**，所以候选分支尚不能称为 production fail-closed；现已补上
`_raise_if_fatal_error()` 及主循环结构测试，使 callback 失败进入不可重试、会写
`RECORDER_ERROR` 的 finalize 路径。

## 3. 已关闭：跨窗口约 40% 计数差异不是有效测量

| session | preflight BidAsk / AllLast / bars | recorder BidAsk / AllLast / bars | 窗口 |
|---|---|---|---|
| RTH | 25,665 / 3,168 / 25 | 15,590 / 2,843 / 25 | 120.109s vs 120.360s |
| OVERNIGHT | 1,620 / 13 / 25 | 923 / 16 / 25 | 120.047s vs 120.078s |

BidAsk：−39.3% 和 −43.0%。AllLast：−10.3% 和 **+23%**。

已核对的事实：两侧订阅参数完全相同（`reqTickByTickData(contract, "BidAsk", 0, False)`）；
两侧计数语义完全相同（都在 `updateEvent` 里遍历 `updated.tickByTicks`）；窗口长度相同；
probe 的 readback 走 `log.read_all()`，覆盖全部 segment。

RTH 两个采样窗口是顺序运行，前一窗口结束到后一进程启动约 1.5 分钟；OVERNIGHT 的间隔
约 6.4 分钟。SPY RTH BidAsk 在这些样本里约为 214 笔/秒量级，报价更新率和成交率并不要求
相邻分钟同向或稳定。没有共同窗口，就不能从绝对计数差异推出丢数据；这组数字从来不是
有效的损失率测量。这里的严谨结论是“测量无效”，不是“差异原因已经解释”。

真正可判定的写路径问题已经用直接对账回答：report 同时记录 callback 侧 `handler_counts`
和落盘 readback `stream_counts`，两者不等时 probe 失败。这能判断“已进入 handler 的事件”
是否在 callback→gzip→readback 路径丢失。

**2026-08-11 RTH 第一层实验已完成：** `clientId=960`、`readonly=True`、`SPY SMART`，
120.406 秒内 handler / raw readback 均为 `8972 / 1707 / 25`，三路逐项完全相等，
`write_path_lost_nothing=true`、`no_swallowed_callback_failure=true`、零 broker write，report
SHA-256 `0cb4c95b86d39e054b3c384bf8a225958609cf4c5dff167b49177ed9c4e02edc`。
这关闭了本窗口中“handler 已收到但 callback→gzip→readback 丢失”的假设。另有本机
million-event soak 在 10,000 events/s 下达到 1,000,000 accepted/persisted/readback、
dropped=0，为写路径提供了独立压力证据。两项合起来证明的是 Recorder 已接收事件的写路径，
不是 IB tick-by-tick 相对于交易所原始 feed 的完整性。

因此不再安排同步 A/B。即使两个 IB 客户端在同窗得到相同计数，也没有交易所级“正确条数”
可对照；若不同，也无法从中定位或修复 IB 的 conflation/分发行为。额外占用两条 market-data
line 与两个 clientId 还会重新引入 `10197` competing-session 风险，而不会改变 B2 决策。

## 4. 历史流程发现：`main` 曾经是红的，而 `STATE.json` 当时失真

- `ae12307` 和 `ef8ec7e` 直接推到 `main`，绕过 PR；CI run 44 / 45 **都是 failure**，
  从 2026-08-09 17:45 UTC 起 `main` 一直红着。
- `ef8ec7e` 的 `STATE.json` 写 `gate_b1: PASS` / `signed_off_commit: 117188ce...`，
  但在该 commit 上 `python -m ib_execution.provenance --check` 报 STALE，
  实际派生值是 `NOT_PASSED` / `None`。
- 原因正是设计要求的：`117188ce..ef8ec7e` 的 diff 含 `src/ib_execution/quote_recorder.py`
  和 `tests/test_recorder.py`，`attestation.validate` 的 `committed <= allowed` 因此为假。

**机制是对的，被绕过的是流程。** 这一轮唯一能自动发现 attestation 失效的哨兵按设计
亮了红灯，然后红灯被忽略了 22 小时。本分支已重新生成 `STATE.json`，套件恢复全绿。

**当前处置（2026-08-12）：** `980f655` 用 `.gitattributes` 固定跨平台工作树字节并重新生成
`STATE.json`，`85f4084` 纳入新的 production liveness 逻辑；在 Windows 重新物化 LF 工作树后，
完整 pytest 与 `provenance --check` 均通过。以上只修复 provenance/测试状态，不自动使 B2 PASS。

### 但这里有一个会持续制造压力的设计问题

现在的 `gate_b1` 字段被迫同时回答两个不同的问题：

- 「B1 在某个 freeze 上通过过吗？」——**是**，`117188ce` 上通过了，这是永久的历史事实，
  `docs/GATE_B1_SIGNOFF_117188cea539.md` 就是它的凭证。
- 「当前工作树被有效 attestation 覆盖吗？」——**否**，而且此后每一个 B2 提交都会让它变否。

用一个字段回答两个问题的后果是：整个 B2 阶段 `gate_b1` 会永远显示 `NOT_PASSED`，
读者会误读成「B1 失败了」，然后**真正的压力会落在那条失效规则上**——最省事的做法
就是把规则放宽，而那条规则是这套 provenance 里最有价值的部分。

2026-08-11 owner 要求“同意则实施”后，已按不放宽验证规则的方式拆成两个派生字段：

```json
"gate_b1_attested_freeze": "117188cea53906665739af3775af64d156856f41",
"gate_b1_covers_worktree": false
```

前者不是靠当前文件“存在”便成立，而是从 Git ancestor 中 metadata-only attestation commit、
该 commit 的 sign-off/evidence blob 与 freeze commit 的 risk config 重新验证，避免后续工作树或
Windows 换行改写历史事实；后者仍由现有 `attestation.validate` 派生，随每次提交重算。

## 5. 一条与现有结论的分歧：1101 不是运气问题，是 probe 设计问题

`GATE_B2_STATUS_20260810_ZH.md` §3 写「没有发现一个明显的、现在即可安全实测但被遗漏的
关键周末 Gateway 场景」，§3.3 写 1101「不应通过反复断网碰运气」。同意不要碰运气，
但我认为这里有一个确定的反例。

`scripts/run_ib_readonly_network_fault_probe.py` 在断网期间**没有持有任何行情订阅**
（脚本里没有 `reqMktData` / `reqTickByTickData` / `reqRealTimeBars`）。而官方语义是
1102 = 恢复且 **market data maintained**，1101 = 恢复但 **market data lost，需要重新订阅**。
没有订阅使该 probe **不能检验“已有 market-data request 丢失后需要重订阅”这个分支**；
因此它对 Recorder 的订阅恢复价值有限。但官方文档只定义 1101/1102 的语义，没有承诺
“无订阅必为 1102”或“持订阅并阻断固定时长必为 1101”。把任何一个码称为唯一可达都过强。

改进配方：在断网前建立 SPY 三路订阅，记录各 stream 最后到达时间、1100/recovery code、
恢复后的首条到达与是否需要 resubscribe；受控解除 outbound block。它仍在只读边界内，
但结果应写成“观察到 1101/1102 中哪一个以及 stream 如何恢复”，不能预注册为必取 1101。
`run_ib_readonly_network_fault_probe.py` 已改为持有三路订阅并记录 recovery 后逐流增量；
`QuoteRecorder.run()` 也已接入 bar heartbeat、1101 重订/1102 不重订、`GAP_SUSPECTED`、
generic tick 49 与 transport timeout re-arm。尚未执行新的防火墙故障轮次。下一轮应把“持订阅
断网”和 production `run()` 的真实 fault 验证合并为同一次 operator-controlled interruption；
单独跑测量 harness 不能关闭 production 路径。

而 1101 本身不是重点。**重点是它之后的状态**：订阅可能已死，socket 仍活着，
`isConnected()` 为 True，没有异常，行情却不再恢复。当前实现用预期每 5 秒到达的 bar 作为
可判定 heartbeat；BidAsk / AllLast 是事件驱动流，其 staleness 只记录、不驱动恢复。这套
production liveness 逻辑仍**从未对着真实 Gateway 故障跑过**。
第 1、2 条说明这个静默失败家族在这个代码库里已经出现过两次；这是同一家族里
还没被实测的第三个成员，也是我认为当前剩余的只读测试里价值最高的一个。

## 下一步建议顺序

1. `.gitattributes` 固定跨平台工作树字节并重新生成 `STATE.json`；完整测试与 provenance
   在 Windows 工作树通过。**（`980f655` + `85f4084` 后已完成）**
2. 旧窗口约 40% 差异按“不是有效测量”关闭，不再安排同步 A/B；handler 对账与 million-event
   soak 已直接证明 Recorder 写路径在各自覆盖窗口内无损。
3. 最高优先级：用真实 `QuoteRecorder.run()` 持三路订阅做一次受控断网，观察实际 recovery
   code、1101 重订/1102 不重订、`GAP_SUSPECTED`、bar heartbeat 与恢复后逐流增量。
4. 运行一次 Full-RTH 全日 health，同时覆盖 `finalize_day`、开/午/收盘 bar cadence、长期内存、
   磁盘和队列水位，并解释 OVERNIGHT route 下 `useRTH=True` 仍有 bar 的直接行为。
5. 完成 Recorder 强杀后 gzip 段级完整性/尾段处置，以及 attestation 统一读取 Git 对象。
6. 完成 `PENDING DOC REVIEW` 并形成 B2 自己的 exact-freeze——不借 B1 attestation 背书。
7. 只读证据封存后，owner 单独决定是否授权 1 股 SPY paper-order 子阶段。

当前没有任何 paper-order 或 live-order 授权，本复核不改变这一点。
