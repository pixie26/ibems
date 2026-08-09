# Gate B2 第一轮真实 IB Gateway 只读证据（2026-08-09）

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
- `tests/test_preflight.py` 验证 pacing，当前 10 tests PASS；
- `git diff --check` PASS。

这项观测说明 B2 不能只测试 broker facts，也必须测试“请求没有 completion”时客户端是否有界失败。

## 5. 没有被本轮证明的事项

- 休市零 tick 不证明 RTH stream 正常或异常；必须在 RTH 重跑 90 秒或更长采样。
- 零持仓、零挂单、零成交时的 hash 稳定，不证明动态 broker snapshot 是原子的，也不证明双快照屏障足以恢复 `SYNCED`。
- 没有验证 Gateway restart、断线重连、1100/1101/1102、late/duplicate/out-of-order callback。
- 没有验证 `orderId / permId / clientId / orderRef`。
- 没有发 paper order；B1 PASS 仍不构成订单授权。

## 6. 仓库状态说明

本轮修改了 B2 preflight 行为与其测试，因此当前工作树不再等同于 B1 exact-freeze source tree。`docs/GATE_B1_SIGNOFF_117188cea539.md` 仍是历史冻结证据，但不能自动证明这些新改动。进入任何 paper-order 测试前，必须把 B2 变更纳入新的可复查 tree 并重新满足相应的回归 / attestation 要求。

Windows 上还观察到一个独立的 provenance 表示问题：`uv.lock` Git 内容未变，但 checkout 的 CRLF 原始字节 SHA-256 为 `4050...`；LF 归一化后为 STATE 记录的 `615629...`。当前 provenance 对 worktree 原始字节取 hash，因此 Linux 生成的 STATE 在 Windows clean checkout 也会显示 stale。这个问题不改变本轮 Gateway 观测，但必须在下一次正式 freeze 前修复，不能通过手改 STATE 掩盖。

## 7. 结论与下一步

本轮结论：**B2 已开始；只读 round 1 部分通过；Gate B2 未通过。**

下一步按顺序执行：

1. 在 SPY RTH 使用 event-driven 计数重跑至少 90 秒，要求 LIVE 且 BidAsk / AllLast / 5s bars 均非零；
2. 在仍为零订单的前提下做受控 disconnect/reconnect 与 Gateway restart，保存前后 snapshot 和完整 error/callback 时间线；
3. 基于真实 completion 行为设计动态 stable-snapshot barrier；
4. 完成官方文档逐项复核后再讨论 1 股 paper order 子阶段。
