# Gate B2 SPY OVERNIGHT 行情与 Recorder 证据（2026-08-10）

## 1. 结论

**OVERNIGHT 子项 PASS；Gate B2 仍未 PASS。**

2026-08-10 香港时间约 08:48–08:59，在真实 IB Gateway paper account、Gateway Read-Only API 开启、Python `readonly=True`、零订单条件下，使用 SPY `exchange=OVERNIGHT` 完成：

1. 120 秒 event-driven 三路行情 preflight；
2. 120 秒真实 `QuoteRecorder` subscription/event-handler/`RawEventLog` 有界写盘试验。

两轮均直接观察到 LIVE market data，BidAsk、AllLast、5 秒 bars 三路均非零。此结论只适用于明确标注的 `OVERNIGHT` destination 和本次 paper Gateway 环境；**不能替代 RTH 验证，也不是 Full-RTH Recorder health report。**

## 2. 为什么必须使用 `OVERNIGHT`，不能使用 `SMART`

第一轮以 `session_label=OVERNIGHT` 但仍使用 `exchange=SMART`，120.219 秒三路均为零。该轮报告正确 fail-closed，但它没有测试 IB 的 overnight destination。

IBKR 官方 API 说明明确指出：overnight 市场数据必须使用与 overnight order 相同的 routing information，并把 contract exchange 设置为 `OVERNIGHT`；overnight 数据不与普通 SMART routed data 重合。参见：

- [IBKR Campus — API Overnight Trading](https://ibkrcampus.com/campus/ibkr-quant-news/api-overnight-trading/)
- [IBKR — Overnight Trading](https://www.interactivebrokers.com/en/trading/us-overnight-trading.php)

因此 preflight 新增了两个显式字段：

- `--session-label OVERNIGHT`
- `--market-data-exchange OVERNIGHT`

并增加机械约束：`OVERNIGHT+SMART` 在连接 Gateway 前直接拒绝；`RTH` 则必须配 `SMART`。标签不会静默改变 route，route 仍由命令显式指定。

## 3. 正确路由的三路行情 preflight

执行边界：

- `clientId=944`
- `readonly=True`
- `session_label=OVERNIGHT`
- contract：`SPY / OVERNIGHT / ARCA / USD`，`conId=756733`
- sample：`120.047s`
- `marketDataType=1`（LIVE）
- `entitlement_blocked=false`

直接观测：

| Stream | 事件数 | 每秒 | 首次到达 | 最后到达 | 最大间隔 |
|---|---:|---:|---:|---:|---:|
| BidAsk | 1,620 | 13.495 | 0.953s | 118.859s | 2.985s |
| AllLast | 13 | 0.108 | 5.406s | 107.516s | 29.641s |
| 5s bars | 25 | 0.208 | 1.203s | 116.781s | 5.235s |

同时完成 account summary 71 项及三轮 `positions/open orders/executions = 0/0/0` 静态 snapshot；`passed=true`。这些账户事实仍然只具有空状态限定。

证据：

- `artifacts/ib_preflight/20260810_b2_overnight_market_v2/report.json`
- report SHA-256：`cda99091b758cbd8fbe442d27bc5132199d6530ac118c94f74be25c8fb202fd7`
- 实际执行脚本 SHA-256：`824004cc59b62ae5ad58a54eb54bd648b7c67def5fda3002871dbca8b29c54c9`
- 同目录 `SHA256SUMS` 固定 report 与实际执行脚本副本。

## 4. Recorder v1 fail-closed 与 pacing 加固

第一次 bounded Recorder 试验没有进入行情采样：Recorder 在刚读取一次 server time 后，以默认 0.2 秒间隔再次连续调用 `reqCurrentTime`，真实 Gateway 在 10 秒 hard deadline 内没有返回 completion，脚本以 `TimeoutError` fail-closed。

这证明之前只加固 preflight 不够；生产 Recorder 的同类请求也必须有一致 pacing。修复为：

- 每个 clock request 前都等待 1.1 秒；
- 包括第一笔，因为 caller 通常刚执行过独立 `reqCurrentTime`；
- 保留同步请求 hard deadline；
- 增加测试验证三次请求之前均实际执行 1.1 秒 pacing。

失败证据保留，不覆盖：

- `artifacts/ib_preflight/20260810_b2_overnight_recorder_v1/report.json`
- report SHA-256：`1b540e28c3b98ac6a0fb42390a8da40a4dcb818d231609e8f0cd7e8349cafb58`
- 唯一 raw SYSTEM segment SHA-256：`e0dca03add40bd26b733e95f3a02ef8b0d290b9e4b121760cf595aee5f6253aa`

## 5. bounded Recorder v2 写盘结果

这不是另写一套计数器。该轮复用实际：

- `QuoteRecorder._subscribe`
- `QuoteRecorder._wire_ticker`
- `QuoteRecorder._wire_bars`
- append-only gzip `RawEventLog`

但不调用按 RTH session 边界计算覆盖率的 `finalize_day`，因此不会把 overnight 数据伪装为 Full-RTH health。

执行边界：

- `clientId=946`
- `readonly=True`
- `exchange=OVERNIGHT`
- sample：`120.078s`
- `broker_write_calls=[]`
- exception：无

从已关闭的 gzip raw segments 重新读取并统计：

| Raw event type | 行数 |
|---|---:|
| BidAsk | 923 |
| AllLast | 16 |
| 5s bars | 25 |
| SYSTEM | 5 |
| 合计 | 969 |

`market_data_types_in_raw_log` 包含 `LIVE`；所有检查为 true，`passed=true`。

证据：

- `artifacts/ib_preflight/20260810_b2_overnight_recorder_v2/report.json`
- report SHA-256：`7c97d75c9da2ac6372f241f721a6038ff4dea326acab869fc3268948869b2e35`
- raw segment 1 SHA-256：`eab5b84ff4664f524e049a7bf13ba231282f64ba7dcda65d791d1082a27e72d5`
- raw segment 2 SHA-256：`c428d7e681139900c4e0952bca9b48edc34bf4908b307e61ac29b578ec3f749e`
- 实际 probe 脚本 SHA-256：`e154aa0b2f799a8c63da728c00d1f7707bbb2cd7fed53e9259175e6afb116062`
- 实际 Recorder source 副本 SHA-256：`bf7d7b54a8d4b1b23a3f1fdd36cf0c53b9bde760cf9bc41d9cbfd6655c4d5321`
- 同目录 `SHA256SUMS` 固定 report、raw segments、lock metadata 和两个实际执行代码副本。

## 6. 验证状态与边界

- preflight 专项：`18 passed`。
- Recorder clock 相关测试：`3 passed`。
- 两个新/修改脚本：Python compile PASS。
- broker-write-path 静态搜索：无 `placeOrder`、`cancelOrder`、`reqCompletedOrders`。
- 曾启动全量 `tests/test_recorder.py`，但时间型测试长时间未结束，受控终止；**不得声称全量 Recorder tests PASS。**
- 本轮没有订单、撤单、completed-orders 请求，也没有关闭 Gateway Read-Only API。
- 第一轮 SMART 零流不能反过来证明 SMART “异常”；它只证明 SMART 不是本次所需 overnight route。
- overnight PASS 不证明 RTH stream，不证明全天 Recorder coverage，不证明订单协议或动态 reconciliation。

## 7. 下一步

后续正式 `RTH+SMART` evidence 已独立完成并 PASS，没有继承 overnight 结论；结果、首轮 10197 失败和 Recorder 写盘证据见 [`GATE_B2_RTH_20260810.md`](GATE_B2_RTH_20260810.md)。两项 bounded PASS 均不能冒充 Full-RTH 全日 health。
