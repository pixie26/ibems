# 架构意见复核与本轮执行记录

日期：2026-08-06  
基线：`v0.1.5.dev0 / Phase 0 reviewed / Specification frozen / Gate B1 not passed`

## 结论

外部建议的主方向正确，应采纳：停止 Phase 0 功能扩张；Gate A 与 execution 分离开发、分别放行；Recorder 因数据不可追回而与 Gate A 并行；B1 只补安全证据；B2 才接 IB 下单协议；MOC、多策略、复杂 reprice 和 watchdog takeover 继续推迟。

但需要六项校正：

1. **代码独立，晋级治理仍耦合。** Gate A 不应进入 execution core，也不应阻塞只读 Recorder；但若 SPY 为 `NO_GO/INSUFFICIENT_EVIDENCE` 且没有独立第二消费者，就不应为一个没有经济用途的系统继续投入 B2 下单适配。
2. **Recorder 是独立 market-data adapter，不是 B2 trading adapter。** 它可以 Week 0 上线，但必须独立进程、clientId、只读会话、节流和有限退避；不能复用 execution event loop。
3. **“双快照”只能是 B2 候选协议。** IB 分别提供 positions end、openOrderEnd、execDetailsEnd；官方没有声明三者构成同一原子时点。因此 B1 只保证“不稳定快照不得恢复 SYNCED”，B2 再测量何种 barrier 能成立。
4. **Windows 不应写成字面 `SIGKILL`。** Python 在 Windows 的强制终止对应 `TerminateProcess`；本轮测试使用 `Popen.kill()`，POSIX 才是 SIGKILL。证据名称统一为 `subprocess force-kill`。
5. **Read-Only 要双层。** Recorder 代码以 `readonly=True` 连接，同时 Gateway/TWS 也应保持 Read-Only API；最好再使用独立 paper username/Gateway。仅靠 Python 对象“不暴露 placeOrder”不是权限边界。
6. **Gate 不由测试数量自动升级。** P/R/A 全覆盖、1,500 examples、真实进程强杀和 fault injection 都只是证据；OS 级 disk-full、真实宿主退出行为及人工评审未完成时继续保持 B1 未通过。

## 官方事实核验

- IBKR 的 tick-by-tick 支持 `BidAsk` 与 `AllLast`；5 秒实时 bar 只能是 5 秒粒度，`TRADES` 才带成交量/WAP/count。实时 bar 还受 market-data line 和小 bar pacing 约束。参考 [IBKR TWS API 文档](https://ibkrcampus.com/campus/ibkr-api-page/twsapi-doc/) 与 [TWS API Reference](https://ibkrcampus.com/campus/ibkr-api-page/twsapi-ref/)。
- delayed market data 不适用于 tick-by-tick；Recorder 必须把 `marketDataType` 和权限错误作为健康判定，而不是“收到数据即视为 LIVE”。参考 [IBKR TWS API 文档](https://ibkrcampus.com/campus/ibkr-api-page/twsapi-doc/)。
- IBKR 明确要求应用关注每日连接维护与 1100/1101/1102；连接恢复不等于订阅和账户状态可信。参考同一官方 TWS API 文档的 Connectivity/System Message Codes。
- Hypothesis 官方支持在 `conftest.py` 注册 profile，以 `--hypothesis-profile` 选择，并以 `--hypothesis-seed` 复现。参考 [Hypothesis settings](https://hypothesis.readthedocs.io/en/latest/tutorial/settings.html) 与 [pytest integration](https://hypothesis.readthedocs.io/en/latest/reference/integrations.html)。
- Python 3.12 在 Windows 对非 console-control signal 使用 `TerminateProcess`，不能把 `/proc`/SIGKILL 语义直接搬到 Windows。参考 [Python 3.12 os 文档](https://docs.python.org/3.12/library/os.html#os.kill)。

## 本轮已执行

### Phase 0 / Gate B1

- 创建隔离的 Python 3.12.13 环境并安装 pytest、Hypothesis、PyArrow、ib_async；
- 修复 watchdog 只支持 Linux `/proc` 的缺陷，Windows 使用 `GetProcessTimes` 做 PID 防复用，强制 fencing 使用 `TerminateProcess`；
- 注册 Hypothesis `gate` profile（1,500 examples），property marker 和可保存 seed/JUnit/hash 的 campaign runner；
- 真正运行默认 Hypothesis，修复此前从未执行到的错误参数；
- 增加七个 subprocess force-kill 窗口：WAL 前、WAL 后/send 前、send 后/ACK 前、partial fill、cancel、stable snapshot 中途、HALT 落盘后；
- force-kill 测试发现并修复“调用方忘记 restore 时重启可清除 HALT”的真实缺陷：现在 connect/reconcile 之前强制 restore；
- 增加 SQLite locked、disk full、malformed WAL、fsync timeout、writer thread death、event-loop/queue 断裂的 fail-closed 测试；
- journal 失败会在内存中设 `HALTED + UNVERIFIED + DEGRADED`、强告警、设置 `fatal_shutdown_requested`，且不会再调用 broker；
- invariant 19 的压力输入/结果/预算写入 intent，auditor 离线重算；invariant 21 的 self-test 进入 Controller 实际构造路径；auditor 声明并检查 1–22。

### Full-RTH Recorder

- 实现真实 `ib_async` 只读连接骨架与可运行 CLI；
- 采集 SPY BidAsk、AllLast、5 秒 TRADES bar、连接/error、marketDataType、server time、local wall/monotonic；
- 使用 IB contract `liquidHours` 确定当日 RTH，支持早收市；
- 独立 token bucket、有限指数退避、独立 clientId 33；
- 原始 gzip JSONL 滚动段、每秒 durability sync、强杀残段保留；
- 收盘后原子生成 `events.parquet`、`health.json`、`manifest.json` 和 SHA-256；
- 健康报告检查 LIVE、三路 stream、RTH coverage、最大 gap、断线、clock skew、行数与 hashes。

## 当前不能声称完成的事项

- 2026-08-07 已在 4002 完成 broker-write-free Gateway 握手；SPY 合约与 server time 正常，但 IB `10089` 明确显示缺少 `SPY ARCA/TOP/ALL` API LIVE entitlement，因此尚未产生真实 SPY RTH 数据；
- positions、all-open-orders、executions 已做三轮顺序读取并观察到两对 canonical hash 相等；该结果只覆盖静态时段，不覆盖成交并发、重连、1101/1102 或 late callback，不能据此判定 B2 stable snapshot 已通过；
- Gate A 应在策略研究仓库独立完成，本仓库只保存经济 Gate 的接口和晋级规则，不复制策略研究代码或结果；
- 正式 Gate profile 已通过并保存：两个生成测试各 1,500 examples、seed `2026080601`、source-tree hash 与 artifact hashes 复核一致；Gate B1 仍按 `docs/INVARIANT_COVERAGE.md` 的其余条件保持未通过。

## 下一步顺序

1. 为当前或独立 recorder paper 会话开通 SPY API 实时行情权限；再次运行 `scripts/run_ib_readonly_preflight.py`，三路 sample 非零后再启动 Full-RTH Recorder。
2. 保留已完成的 1,500-example Gate campaign；随后做 OS/卷级 disk-full 与宿主进程退出演练。
3. Gate A 独立给出 `GO_TO_DATA_VALIDATION / NO_GO / INSUFFICIENT_EVIDENCE`。
4. 只有 B1 正式签字、且 Gate A/第二消费者支持继续投入时，才进入 B2 的只连接/只读 snapshot protocol；最后才做人为 1 股 target/cancel。
