> **Historical document. Superseded by [`FINAL_EXECUTION_PLAN_ZH.md`](FINAL_EXECUTION_PLAN_ZH.md).**
> Counts and implementation status below describe the earlier review stage.

# 优化后的设计与执行方案

## 一、项目决策顺序

平台与 SPY 策略必须拆成两个独立决策：

```text
Operational Gate：平台能否安全运行？
Economic Gate：策略在真实成本和不确定性下是否值得交易？
```

平台完成不能成为 SPY 上线理由；SPY no-go 也不必否定一个有明确第二使用者的平台。

当前第二使用者应记录为：

```text
NONE_CONFIRMED
```

QQQ 与 SPY 属于高度相近的日内动量命题，不能单独证明平台是策略无关资产。若 Gate A 为 no-go/证据不足，且没有点名另一项独立策略，则停止在 recorder + reusable FakeBroker core，不继续投入 IB adapter。

## 二、并行轨道

### Track A — 经济性否证，最高优先级

使用现有回测数据，先完成：

1. SEC Section 31 point-in-time 费率序列；
2. FINRA TAF、commission、borrow、financing 的 PIT 口径核对；
3. 冻结一个可交易的 EOD 成交规则和价格序列；
4. state-independent 与保守 tail stress 两套成本；
5. circular block bootstrap / HAC；
6. 明确回答：
   - post edge 是否为正；
   - pre-post 差异的不确定性；
   - 保守成本下是否仍有正期望；
   - 证据是否足以支持最小 live 数据实验。

Gate A 的输出只能是：

```text
GO_TO_DATA_VALIDATION
NO_GO
INSUFFICIENT_EVIDENCE
```

`INSUFFICIENT_EVIDENCE` 对资金部署等同于 no-go，但可以允许 recorder 继续采集。

### Track R — Recorder，日历约束，立即并行

全 RTH 记录：

```text
BidAsk tick-by-tick
AllLast tick-by-tick
5-second TRADES bars
connection/system events
IB server time
local wall + monotonic arrival time
```

原则：

- read-only；
- 优先独立 Gateway / paper session；
- 如共享基础设施，必须有独立 token bucket 和 bounded exponential backoff；
- 日检自动推送；
- 原始事件 append-only；
- 收盘后 compact，不删除 raw；
- 每日 manifest/hash。

Recorder 的第一版验收：

```text
market_data_type 始终 LIVE
三路 required streams 均存在
RTH coverage >= 99%
最大未解释 gap <= 30 秒
本地与 IB server clock skew <= 2 秒
断线后重订阅经测试
同日重启不覆盖旧 segment
```

## 三、执行平台 Gates

### Gate B0 — 规格冻结

冻结：

- 四维状态向量；
- coupling rules；
- journal event schema；
- exact ownership；
- at-most-once；
- reprice policy；
- EOD/risk/manual flatten 区别；
- 21 invariants；
- failure matrix；
- risk config schema；
- single writer 边界。

冻结后只有两类改动可以修改 spec：

1. 发现某条不变量不能保证安全；
2. 实测 IB 行为与假设不符。

### Gate B1 — IB-free deterministic core

必须完成：

1. FakeBroker fault matrix；
2. event replay 恢复；
3. atomic target acceptance；
4. atomic execution booking；
5. exact durable identity；
6. runtime risk counters restart-safe；
7. AsyncControllerBridge；
8. 21 条三重 invariant coverage。

每一条 invariant 都要有：

```text
property test
runtime assertion / structural enforcement
offline journal auditor
```

验收：

```text
0 duplicate broker submission under generated interleavings
0 external fact silently adopted
0 send/cancel while untrusted
0 journal claim-without-event gap
replay state == online terminal state
all 21 coverage rows COMPLETE
all generated sessions auditor PASS
```

Phase 0 最多四周。到期未过：

1. 先砍 reprice ladder，退化为 single attempt + abandon；
2. 再砍自动 EOD ladder，保留人工 paper 测试；
3. 不得通过删安全检查来“赶进度”。

### Gate B2 — IB paper protocol verification

范围：

```text
独占 paper account
SPY
1–5 股
manual target
marketable limit + market
无 MOC
```

先实现 documented-vs-observed matrix：

```text
orderRef 是否完整进入 executions
permId 跨 session 稳定性
orderId/clientId 行为
openOrders / reqAllOpenOrders 差异
1100/1101/1102 实际回调顺序
partial fill 与 fee callback 顺序
execution correction 格式
cancel reject/fill race
Gateway daily restart
snapshot 是否需要两遍稳定读取
paper fill 规则
```

真实 snapshot 建议采用稳定屏障，而不是假设 positions/open orders/executions 原子一致：

```text
snapshot A
等待 API end markers
短暂 quiet period
snapshot B
A/B identity 与 position 一致才接受
否则继续 SYNCING / HALT
```

具体协议以 Gate B2 实测为准。

### Gate B3 — 自动故障演练

至少包括：

```text
kill -9 after intent commit
kill -9 after SEND_ATTEMPT_STARTED
kill -9 after placeOrder return
kill -9 after partial fill
kill -9 during cancel
Gateway restart
1101 resubscription
journal write failure
bridge queue overload
callback handler exception
external order/execution
EOD residual
stale snapshot
risk config corruption
runaway reject loop
```

Watchdog：

```text
可以：告警、SIGTERM、经身份验证后 SIGKILL
不可以：重启、下单、改 mode
```

自动 kill 前必须解决 stale status / PID reuse，不能只信一个旧 PID。

### Gate C — Shadow signal

#### L1 确定性

```text
同一 raw capture -> live/replay 每根 bar、feature、target 100% 一致
```

#### L2 语义差异

逐条比较 IB live 与 research parquet：

```text
timestamp convention
bar boundary
OHLCV
odd lot / trade condition
session boundary
prev close/dividend
cumulative VWAP
band
signal
shares
```

每个 material difference 必须归类并量化 P&L 影响。

#### L3 数据源敏感性

- L3a：IB historical bars，仅用于证伪；先验证历史深度和 pacing；
- L3b：Week 0 起保存的 live raw feed；
- 参数不重调；
- full/pre/post 和 tail-day attribution 全部重跑。

L3a 通过不代表 L3b 通过。

### Gate D — 最小 live 成本实验

进入条件：A、B1、B2、B3、C 均通过。

初始：

```text
1 股
无杠杆
独占账户
按隔夜风险设 max position
无 MOC
硬订单数/累计成交/名义金额上限
```

记录：

```text
decision time/price
arrival bid/ask/mid/size
send/ack/fill latency
partial fills
arrival shortfall bps
future 1s/3s/5s adverse move
spread/vol/gap/time bucket
reject/miss reason
shortable/borrow state
```

独立记录：

```text
DECISION_MISSED(DISCONNECTED/NOT_SYNCED/RISK_BLOCKED/EXPIRED/DATA_STALE)
```

### Gate E — 成本模型与资金决策

分三层：

```text
Deterministic fees
Observable micro-size execution cost
Modeled size/capacity cost
```

尾部成本采用不对称更新：

```text
实测坏于 stress -> 立即提高成本 / 可停止
实测好于 stress -> 不下调，继续采样
达到预注册 tail N 与区间宽度 -> 才可考虑下调
```

5 股可以测 latency、half-spread、arrival shortfall、reject/miss；不能证明 market impact、queue depletion、auction capacity。

## 四、当前实施顺序

```text
Now
  A1 现有数据成本补完 + bootstrap
  R1 recorder 真实订阅实现与上线
  B1 修完 invariant coverage / async bridge / auditor

若 Gate A = NO_GO 或 INSUFFICIENT_EVIDENCE
  SPY 不接入
  若 second_consumer == NONE_CONFIRMED
      停止 IB adapter；保留 recorder 与 FakeBroker core

若 Gate A = GO_TO_DATA_VALIDATION
  验证历史深度/pacing
  Gate B2 paper
  Gate B3 failure drills
  Gate C L1/L2/L3
  Gate D 1-share live
  Gate E 独立资金决策
```

## 五、语言和技术栈

```text
Python 3.12+
ib_async
asyncio event loop
AsyncControllerBridge
single controller worker thread
SQLite WAL / synchronous FULL
PyYAML strict config
pytest + Hypothesis
```

选择 Python 的核心原因不是“性能够用”，而是：

- 单一串行状态 writer；
- 更容易做 FakeBroker、replay、property test 和 fault injection；
- 与研究栈共享数据类型；
- 系统延迟权威在 IB Gateway，而不是 Python 指令执行。

安全条件：任何阻塞 I/O 不得直接运行在 IB event loop。
