# Gate B2 第一轮真实 IB Gateway 只读证据（2026-08-09）

> 当前状态入口：[`GATE_B2_STATUS_20260810_ZH.md`](GATE_B2_STATUS_20260810_ZH.md)。本文保留各轮实验过程、直接观测和证据细节；截至 2026-08-10，Gate B2 仍未 PASS。

## 1. 范围与安全边界

- Gateway：IB Gateway paper account，本机 `127.0.0.1:4002`。
- 客户端：`ib_async==2.1.0`，Python 3.12.13，Windows 11。
- API 会话：独立 `clientId=933`，`readonly=True`，`StartupFetchNONE`。
- 本轮没有调用 `placeOrder`、`cancelOrder` 或任何其他 broker write。
- 报告不保存 account id、余额、持仓明细或订单明细；只保存数量和 canonical SHA-256。
- 本轮是周日休市测试，不具备 RTH 行情覆盖资格。

## 2. 可复查证据

- 报告：`artifacts/ib_preflight/20260809_b2_round1/report.json`
- 报告 SHA-256：`e37ea03c9c48b91426ced9c9b6d6bd41979778924ba90fc96bfdb28d8788f3cc`
- 干净断开后重连报告：`artifacts/ib_preflight/20260809_b2_round1_reconnect/report.json`
- 重连报告 SHA-256：`23b4503426dfae90c1df25692d9db16d3dc813e0843486d5e11573d6b03d6d21`
- 开始：`2026-08-09T15:38:07.868937Z`
- 完成：`2026-08-09T15:38:34.872712Z`

执行命令：

```powershell
.\.venv312\python.exe scripts\run_ib_readonly_preflight.py `
  --port 4002 `
  --client-id 933 `
  --request-timeout 10 `
  --snapshot-rounds 3 `
  --snapshot-interval 1 `
  --market-data-timeout 10 `
  --sample-seconds 15 `
  --output artifacts\ib_preflight\20260809_b2_round1\report.json
```

脚本退出为非零，因为总 gate 要求三路行情均非零；休市时该条件不满足。这是正确的 fail-closed verdict，不是本轮连接失败。

## 3. 直接观测

| 检查 | 结果 | 观测 |
|---|---|---|
| TCP 4002 | PASS | `TcpTestSucceeded=True` |
| Read-only API handshake | PASS | connected；server version 178 |
| Managed account | PASS | count 1；id 未写入报告 |
| Broker clock | PASS | 7 样本；median `+0.517s`；max abs `0.837s` |
| SPY qualification | PASS | 唯一合约；`conId=756733`；details count 1 |
| Static broker snapshot | PASS（候选证据） | positions/open orders/executions 三轮均为 0；两对连续 snapshot hash 相等 |
| LIVE entitlement | PASS | `marketDataType=1`；`entitlement_blocked=false` |
| BidAsk | 未形成 RTH 结论 | 周日 15.234 秒计数 0 |
| AllLast | 未形成 RTH 结论 | 周日 15.234 秒计数 0 |
| 5s bars | 未形成 RTH 结论 | 周日 15.234 秒计数 0 |
| Overall script verdict | EXPECTED FAIL-CLOSED | `passed=false`，唯一未通过项为 `all_streams_nonzero` |

IB informational status 包括 market-data farm healthy / connecting、historical farm inactive-on-demand 和 sec-def farm healthy；没有 354、10089、10189、10197 或 market-data-permission 420。

完成第一轮正常断开后，使用同一 `clientId=933` 建立了新的只读 API 会话。重连成功，server version、account count、SPY `conId` 均保持一致；三轮静态 snapshot 仍为 `0 / 0 / 0`，整体 hash 仍为 `50ae5b426df87ac7a7a6ee7eec9bd0c81719ad7772be6cf4ced524fd30fcab91`。这只证明干净 client disconnect 后可以重新连接并读到相同的静态事实，**不等于**网络中断恢复或 Gateway restart。

## 4. 本轮发现：同步 `reqCurrentTime` 可无限等待

第一次和第二次组合 preflight 分别被 60 秒、180 秒外层限时回收，均未生成报告。定时 traceback 两次稳定定位在：

```text
ib_async.ib.IB.reqCurrentTime
scripts/run_ib_readonly_preflight.py:measure_clock_skew
asyncio.windows_events._poll
```

原因边界已经直接观察到：

- 单次 `reqCurrentTime` 正常；
- 原脚本以约 0.2 秒间隔重复请求时，至少一次请求没有收到 completion；
- `ib_async 2.1.0` 的 `IB.RequestTimeout` 默认是 0，因此 blocking wrapper 没有 deadline；
- 设定同步请求硬超时并把 clock 请求最小间隔改为 1.1 秒后，连续 7 次返回；完整 preflight 在 29.1 秒内完成。

已做机械加固：

- 所有同步 IB 请求使用 `IB.RequestTimeout=10s`（可通过 `--request-timeout` 下调或显式调整，禁止 0）；
- clock 请求之间至少间隔 1.1 秒；
- `tests/test_preflight.py` 验证 pacing 与请求超时的 UNKNOWN 表示，当前 12 tests PASS；
- `git diff --check` PASS。

这项观测说明 B2 不能只测试 broker facts，也必须测试“请求没有 completion”时客户端是否有界失败。

## 5. 2026-08-10 休市期补测

### 5.1 Account summary 与 read-only order-information 边界

扩展 preflight 后，真实 Gateway 的 `reqAccountSummary` 在 `0.141s` 内收到 completion，返回 71 项；报告只保存数量与 canonical hash，不保存账户名或余额。

随后尝试 `reqCompletedOrders(apiOnly=False)` 时出现了新的重要边界：

- 第一次请求在 10 秒内没有收到 `completedOrdersEnd`，客户端按硬 deadline 抛出 `TimeoutError`；这不能解释为“零条 completed orders”，只能记为 `UNKNOWN`；
- 再次开始复核时，Gateway 弹出要求更改 Read-Only API 设置的提示；操作者没有关闭只读设置，测试立即终止；
- 脚本没有调用 `placeOrder` 或 `cancelOrder`，事后复核也仍为零持仓、零挂单、零成交；
- IB 官方旧版 TWS API setup 文档明确说明：启用 Read-Only 时 API 无法取得 order information。这与本次直接观测一致。因此 `reqCompletedOrders` 已从默认零写入 preflight 路径移除，并明确记录为 `BLOCKED_BY_GATEWAY_READ_ONLY_POLICY`，不能为了补齐读取覆盖而关闭保护。

操作者随后使用隔离的最小复现命令再次调用同一个请求，Gateway 稳定复现提示：

> 某API客户端正在尝试发送需要API写入权限的请求。要允许此操作和后续类似操作，请在全局配置的API/设置下取消勾选“只读API”复选框。

复现会话使用 `clientId=939`、`readonly=True`、`StartupFetchNONE`，只调用 `reqCompletedOrders(apiOnly=False)`；复现代码不包含 `placeOrder` 或 `cancelOrder`。因此该截图证明的是 **Gateway 将此请求分类并拦截为需要 API 写入权限**，不是“API 已提交订单”的证据。严格只读 B2 不接受取消 Read-Only；该请求保持 blocked。

提示截图及结构化说明：

- `artifacts/ib_preflight/20260810_readonly_completed_orders_prompt/gateway_readonly_prompt.png`
- 截图 SHA-256：`63c4ecd568d97563949cdc43011020b273eb243f9daa9f9bc58ec3f3ceb98ddc`
- `artifacts/ib_preflight/20260810_readonly_completed_orders_prompt/observation.json`

安全复核报告：

- `artifacts/ib_preflight/20260810_b2_post_prompt_safety_check/report.json`
- SHA-256：`7b8d0e5fe1491feed70a7b1b7578eea8da4f597b05ed021e07132e48c576cede`
- account summary：71 项；三轮 positions/open orders/executions 均为 `0 / 0 / 0`，整体 hash 一致；
- `passed=false` 仅因为休市期三路行情计数为 0，不是账户事实或连接检查失败。

### 5.2 两个 client 与只读 client 异常死亡

新增的故障探针不包含下单、撤单或 completed-orders 请求。直接观测结果：

- `clientId=934` 与 `935` 同时连接成功，均为 `readonly=True`；
- 两个 client 读取的 positions/open orders/executions 均为 `0 / 0 / 0`，snapshot hash 相同；
- 第一个 `clientId=937` 保持连接时，第二个同 ID 连接被拒绝并返回错误 326；没有同时存活的两个同 ID 会话；
- `clientId=936` 的子进程在已连接后被 Windows 强制终止，没有执行正常 `disconnect`；
- 第一次重连尝试即成功，约 `0.110s` 后同一 `clientId=936` 可重新连接；恢复快照仍为 `0 / 0 / 0`。

证据：

- `artifacts/ib_preflight/20260810_b2_client_fault_v2/report.json`
- SHA-256：`a20f51f843330ef67a2f8ba955059905980ee2a19a94e0338824bd26e59c61ac`

这证明 API client 进程死亡后 Gateway 释放了该 client ID，且静态零事实可重新读取；该轮本身不证明 Gateway restart、外网断线、动态订单 reconciliation 或 cross-client order visibility，其他故障形态见后续独立轮次。

### 5.3 Gateway 正常退出、Task Manager End task 与 `TerminateProcess`

在仍为零订单且 Read-Only API 保持开启的条件下，分别完成了三个独立场景：

1. Gateway 正常退出后重新启动；
2. Windows Task Manager `End task` 后重新启动；
3. 对精确的 `ibgateway.exe` PID 执行 `Stop-Process -Force`，即 Windows `TerminateProcess` 级终止，然后重新启动。

三轮均直接观察到 API socket 断开，随后使用原 client ID 重新连接，并在连接后重新请求 positions、all-open-orders、executions。结果：

- 正常退出：`clientId=938`；重启前后均为 `0 / 0 / 0`，snapshot hash 相同；
- Task Manager End task：`clientId=940`；重启前后均为 `0 / 0 / 0`，snapshot hash 相同；由于 Gateway 表现出保存/退出过程，这一轮不声称是无清理窗口的 hard kill；
- `TerminateProcess`：`clientId=941`；重启前后均为 `0 / 0 / 0`，snapshot hash 相同；
- 三轮重启过程中都出现错误 10141，表示 paper API 在 disclaimer 被接受前尚未 ready；
- 正常退出轮前两次连接各 10 秒超时，第三次完成连接和 snapshot；
- Task Manager End task 轮第一次恢复尝试 10 秒超时，第二次在约 `0.156s` 内同时完成连接和 snapshot；
- `TerminateProcess` 轮前两次恢复尝试各 10 秒超时，第三次在约 `0.140s` 内同时完成连接和 snapshot；
- 直接观测证明 API handshake、paper disclaimer 与 broker-state request completion 是不同阶段。恢复判定因此被加固为：**同一 client ID 连接成功且完整 broker snapshot 成功**，仅 socket connected 不足以恢复为 ready/synced。

正式成功证据：

- 正常退出：`artifacts/ib_preflight/20260810_b2_gateway_restart_v3/report.json`
- SHA-256：`90fc489e55d23e69f7295827950945e85c3f25b52d8d2c6607600afefc633329`
- Task Manager End task：`artifacts/ib_preflight/20260810_b2_gateway_hard_kill_v2/report.json`
- SHA-256：`b954367be4f976acf3ffca02eb7e4e71bb82eac1dadb609b5a64128ea5b4c441`
- `TerminateProcess`：`artifacts/ib_preflight/20260810_b2_gateway_terminateprocess/report.json`
- SHA-256：`2097c4fb5461b15a523879ee4cc7b7765e204db4d38d921a9a8b0e4c457a52d8`

本地 Gateway 正常退出、End task 和 `TerminateProcess` 产生的是 socket EOF / `ConnectionError`，没有观察到 1100/1101/1102；这与下面“Gateway 进程保持运行、仅中断其外网连接”的故障形态不同。

### 5.4 Gateway 进程存活时的受控外网断线 / 恢复

2026-08-10 使用 Windows Defender Firewall 创建唯一、临时、仅针对 `D:\tws\ibgateway\ibgateway.exe` 的 outbound block；没有关闭网卡、没有阻断其他进程、没有关闭 Gateway，也没有改动 Read-Only API。规则在 `2026-08-09T17:01:59.4564022Z` 创建，45 秒后清理；operator console 显示 `RuleStillPresent=False`，随后本机再次确认该规则不存在。Gateway 全程保持原 PID `33988`。

只读观测器 `clientId=942` 的直接结果：

- 故障前完成 broker server time 与 positions / all-open-orders / executions 快照，计数为 `0 / 0 / 0`；
- `17:02:02Z` 先收到 market-data farm 断开 2103，`17:02:08Z` 收到真实 1100；
- 本地 API socket 在外网故障期间保持连接；
- firewall 清理后，先收到 farm 恢复，`17:03:35Z` 收到 1102（连接恢复、数据保持）；本轮没有收到 1101；
- 1102 后重新请求 broker server time 成功，并重新完成 positions / all-open-orders / executions 快照；计数仍为 `0 / 0 / 0`，前后 snapshot hash 相同；
- `broker_write_calls=[]`；没有订单、撤单或 completed-orders 请求。

判定：**此项 PASS。** 这直接验证了该 paper Gateway build 在“进程存活、外网短暂中断”场景下的 1100 → 1102 路径，以及恢复后只读空状态 reconciliation completion。它不证明 1101 路径，不证明持仓/挂单/成交非空时的动态 reconciliation，也不证明 late/duplicate/out-of-order order callback。

证据：

- `artifacts/ib_preflight/20260810_b2_gateway_network_fault_v2/report.json`
- report SHA-256：`f8c88498a74402d59a49f80a0cdf9b61903a8d361b80b7045457f467f30c8bc2`
- Gateway reconnect banner screenshot SHA-256：`6f470c8f80ee17492738b1270e4d86a016031ee366b52d3b2a2ece4ddac8a5a6`
- Gateway connection-status screenshot SHA-256：`2b25e9447cda566d75f3effa77530489e963e0c76cb391fe49bed4db7fd7b8ff`
- operator console transcript 明确标注为用户提供内容的逐字转录，不冒充 probe 自动输出。
- `SHA256SUMS` 同时固定 report、截图、转录以及本轮实际执行的 Python / PowerShell 脚本副本。

### 5.5 SPY OVERNIGHT 行情与 bounded Recorder

2026-08-10 香港时间约 08:48–08:59，使用 IBKR API 明确要求的 `exchange=OVERNIGHT` 完成两轮 120 秒只读测试：

- event-driven preflight：BidAsk / AllLast / 5s bars 为 `1620 / 13 / 25`，`marketDataType=1`，`passed=true`；
- 实际 Recorder subscription/event-handler/`RawEventLog` 写盘：`923 / 16 / 25`，raw event 合计 969，`passed=true`。

第一轮错误使用 SMART route 时三路为零；IBKR 官方 API 文档确认 overnight data 与普通 SMART routed data 不重合。preflight 已加固为 `OVERNIGHT` label 必须显式配 `OVERNIGHT` exchange。Recorder v1 还直接暴露了 0.2 秒 clock pacing 的 callback timeout，随后与 preflight 统一为每次请求前 1.1 秒并成功重跑。

详细证据、失败轮次和全部 digest 见 [`GATE_B2_OVERNIGHT_20260810.md`](GATE_B2_OVERNIGHT_20260810.md)。此项判定为 **OVERNIGHT PASS**，但不证明 RTH 或 Full-RTH health。

### 5.6 SPY RTH 行情与 bounded Recorder

2026-08-10 香港时间约 23:01–23:07，正式 `RTH+SMART` preflight 与真实 Recorder bounded 写盘均运行超过 120 秒：

- preflight v2：BidAsk / AllLast / 5s bars 为 `25665 / 3168 / 25`；
- Recorder：关闭 gzip 后独立重读为 `15590 / 2843 / 25`，所有市场数据行均为 LIVE。

首轮在约 68 秒出现真实 `10197` competing-session error，并暴露 `entitlement_blocked=true` 仍可能错误汇总为 PASS 的工具缺口。现已增加 fatal entitlement 与完整 sample-window 两个否决检查；失败 v1 保留，设置调整后的独立 v2 才作为 RTH PASS 证据。详细结果和 digest 见 [`GATE_B2_RTH_20260810.md`](GATE_B2_RTH_20260810.md)。这仍不是 Full-RTH 全日 health。

### 5.7 持三路订阅的 production 受控断网（2026-08-12）

真实 `QuoteRecorder.run()` 在持有 BidAsk、AllLast、BAR_5S 时执行一次经 owner 明确授权的 45 秒
`ibgateway.exe` outbound block。直接观察 1100→1102、connection epoch 保持 1、没有
`RESUBSCRIBE_REQUIRED`；1102 后三路本地接收分别约 +0.264/+0.267/+0.896 秒。没有观察到 1101，
因此 1101→重订阅仍未验证。

首次启动因 STK generic tick 49 被 Gateway error 321 拒绝而 fail closed；失败 artifact 保留。重试使用
`market_data_generic_ticks=""`，约 13:00 ET 才开始，所以是部分日长跑，不是 Full-RTH。旧进程为同一
outage 每 0.25 秒写 marker，共 380 条；新 incident 生命周期随后在源码中修正并通过测试，但当前真实
进程加载旧代码，不能把修正倒推为真实 Gateway PASS。完整记录见
[`GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md`](GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md)。

## 6. 没有被本轮证明的事项

- RTH bounded 三路行情已经证明；两分钟样本仍不证明完整交易日 coverage 或整日 gap thresholds。
- 零持仓、零挂单、零成交时的 hash 稳定，不证明动态 broker snapshot 是原子的，也不证明双快照屏障足以恢复 `SYNCED`。
- Gateway 正常退出、Task Manager End task、`TerminateProcess`，以及空状态和持三路订阅 production run 的外网断线已经验证；两类外网断线均直接观察到 1100→1102，但尚未观察到 1101，也未验证 late/duplicate/out-of-order order callback。
- 没有验证 `orderId / permId / clientId / orderRef`。
- 没有发 paper order；Read-Only 权限提示也不构成订单尝试或订单授权；B1 PASS 仍不构成订单授权。

## 7. 仓库状态说明

本轮修改了 B2 preflight 行为与其测试，因此当前工作树不再等同于 B1 exact-freeze source tree。`docs/GATE_B1_SIGNOFF_117188cea539.md` 仍是历史冻结证据，但不能自动证明这些新改动。进入任何 paper-order 测试前，必须把 B2 变更纳入新的可复查 tree 并重新满足相应的回归 / attestation 要求。

Windows provenance 的 checkout 行尾问题已由 `.gitattributes` 的 `* -text` 修复；随后又发现生成器本身
用 `Path.write_text()` 在 Windows 重新产生 CRLF。生成器现改为显式 UTF-8 bytes + LF，并有回归测试；
当前 `STATE.json` 实测 `CR=0` 且 `python -m ib_execution.provenance --check` 通过。该修复只保证表示一致，
不把新的 B2 worktree 纳入历史 B1 attestation。

历史 2026-08-10 Windows 全套失败证据仍保留，不能改写。但相关 Windows 目录 fsync、Recorder 和
provenance 问题后续已修正；2026-08-12 当前 B2 工作树收集 386 tests，完整运行 381 PASS、5 个环境相关
SKIP，并通过核心 Ruff、PowerShell parse、`git diff --check` 和 provenance `--check`。这是当前工作树的
回归证据，不是新的 B1 exact-freeze 签字。

## 8. 结论与下一步

本轮结论：**B2 已开始；只读 round 1 部分通过；Gate B2 未通过。**

下一步按顺序执行：

1. 周末、零订单、Read-Only 边界下有明显安全价值的主要 Gateway 实测已经完成；completed-orders 保持 `BLOCKED_BY_GATEWAY_READ_ONLY_POLICY`，不得为补测而关闭 Read-Only；
2. 明确 `OVERNIGHT` destination 的 SPY 行情与 bounded Recorder 已完成并 PASS；该结果不能替代 RTH；
3. SPY RTH event-driven preflight 与 bounded Recorder 已完成；仍不得把两分钟样本称为 Full-RTH 全日 health；
4. 持三路订阅 production 1100→1102 已观察；保留 1101 为尚未观察到的分支，不得由 1102 推断，也不得为取码反复断网；
5. 当前部分日自然结束后封存 accounting/health/manifest；另一天从开盘前运行真正 Full-RTH；
6. 完成官方文档逐项复核，并把 B2 source、tests、docs 和 evidence 纳入新的可复查 freeze；
7. 只读证据封存后，再由 owner 单独决定是否授权 1 股 SPY paper-order protocol；非空 dynamic snapshot、订单身份和订单 callback 均留在该子阶段。
