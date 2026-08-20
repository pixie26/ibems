# 2026-08-18 Full-RTH 证据跟进（2026-08-20）

## 1. 结论与边界

本次在 Windows 运行主机上只读核验既有 artifact，没有重跑 Recorder、复制 raw、创建大型测试文件或改写既有证据。

- **verified**：运行由 `agent/full-rth-v4-repair@34f3ac43cc56982976e401fd519512b2462e7e35` 启动，launch record 显示工作树干净；其 source/config/dependency 摘要与该 Git 对象中的 `STATE.json` 完全一致。
- **verified**：运行边界为 `READ_ONLY`、`order_authorization=NONE`、`trading_adapter=NOT_IMPLEMENTED`；最终 runtime phase 为 `FINALIZED`，v3 为 FAIL、v4 为 PASS，原 Gate 未改变。
- **partially verified**：v3 health、v3 manifest 与 Parquet 文件仍存在，但当前审计进程受其 Windows ACL 限制，不能重新读取并直接计算摘要；下表保留运行时 amendment/stdout 登记值并明确标注来源。
- **not Gate PASS**：v3/v4 判定权和 writer-lag OPEN 项仍需 owner 裁决；本文件不升级 Gate B2 或 Full-RTH 状态。

本次 evidence root 共 95 个文件、103,515,782 bytes，其中 82 个 gzip segment 共 57,486,838 bytes，`events.parquet` 为 45,901,319 bytes。未新增 raw 或 Parquet。

涉及账户或客户端的敏感标识只保留在本机原始证据中，不写入仓库、报告或会话输出。

## 2. 精确运行树

| 项目 | 核验值 |
|---|---|
| Git commit | `34f3ac43cc56982976e401fd519512b2462e7e35` |
| Git branch | `agent/full-rth-v4-repair` |
| launch 时 worktree changes | `0` |
| source tree SHA-256 | `e1894c9c33d8cc469c2da55bcc6a74f11a0bfc497cfd5c6af0f436958f4b8f2b` |
| config tree SHA-256 | `2afcb80305ec6de198a911624c05f6e7681326d16d53fe0a268e60594ab26dcf` |
| dependency lock SHA-256 | `6156296dd9b10927a0700cc2dfd77d42a19a6c39c0bb2753d982b30045de1a5b` |
| Task XML SHA-256 | `aea8636a8d5347a690132e173b69433d2f12f65de0826a03268a1015facef8fc` |

`task-launch.json` 的三个 tree/lock 摘要已与 `git show 34f3ac4:STATE.json` 逐项比较；不是仅依据报告文本推断。

## 3. 小型控制证据的直接摘要

以下摘要由 2026-08-20 审计进程直接读取磁盘文件并计算：

| 文件 | bytes | SHA-256 |
|---|---:|---|
| `task-launch.json` | 3,718 | `12f0c1e8b3730e753f53dabda391f581fa624c6426a9933efaf37a85ac0d9e9b` |
| `launch-decision-note.md` | 2,767 | `762d7b75d64caea379ecd644f3d64c838fc38bf237d5214f0065a782ac17a165` |
| `task-runtime-status.json` | 922 | `66baf7a79520c43b52525ea2ada2a634a506d93918f2c177267af93069cbdf95` |
| `recorder-status.json` | 615 | `d3fe3ff44231e96d84535c749630ec4734b623eb515d66d57238852ff1123693` |
| `recorder-stderr.log` | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `recorder-stdout.log` | 27,320 | `ef7a66b4ebab772edc2643f33a4227b241dbc251bf90ab169e800de031b6df97` |
| `health-v4.json`（磁盘实际 bytes） | 13,029 | `6071625b87d3e5af80467e1f3d901721fadf41b343338e2438bf44726ff73d85` |
| `manifest-amendment-v4.json`（磁盘实际 bytes） | 28,114 | `07c142224724be433148b94b42efb01d3305495878b6ca0e3ea1ec2773b13451` |

## 4. 受 ACL 限制的证据

| 文件/集合 | 登记 SHA-256 | 来源与验证状态 |
|---|---|---|
| `health.json` (v3) | `b6c11b6079370cd9a78cb7e1a350fc1ce285bfa5ccbae931a2eb63ffa6c53049` | v4 amendment 在重分析前读取原文件后登记；当前不能直接重算，**partially verified** |
| `manifest.json` (v3) | `91124f20ffad4b08a491b502ea676d6a36719beeb866076d2db6ac4ce7b03249` | 同上，**partially verified** |
| `events.parquet` | `210a89b9e269accce59772b00e768819094681bbe8e091c5a33155f69a0e18aa` | Recorder stdout 中的 manifest 输出；当前不能直接重算，**partially verified** |
| 82 个 raw segments 聚合摘要 | `135a8bd3b495e444d667f1c09ff76ef808d9c29b6dfdcdf319cabf60fb65f527` | v4 输入 metadata；inventory match、前后摘要一致、两次 compressed scan 和一次 semantic decode 均记录为 true，**verified by v4 pipeline, not independently rehashed in this follow-up** |

不得通过改 ACL、复制或改写原文件来补齐该缺口。若未来需要独立复核，应由 owner 明确授权一个只读 ACL/备份导出流程，并对导出副本和源文件同时登记摘要。

## 5. Windows v4 摘要缺陷与修复

跟进发现 amendment 的 `health_v4_sha256` 为
`6a36057980c4f1297554ba03660c2b73a69d3c65b68726e686aaf55bf6fd64e4`，与 §3 的磁盘摘要不同。

差异已被严格界定为换行转换：磁盘文件有 173 个 CRLF；仅把 CRLF 还原为 LF 后，长度从 13,029 变为 12,856 bytes，SHA-256 精确等于 amendment 记录值。没有其他内容差异。

根因位于 `recorder_health_v4._create_durable()`：Windows 的 `os.open()` descriptor 默认是 text mode，`os.write()` 会把待写 bytes 中的 LF 扩展为 CRLF，而摘要在写入前按 LF bytes 计算。现有 create-only 证据不改写；本文件同时登记磁盘实际摘要与 amendment 的预写入摘要。

修复在本文件所在变更中给 `os.open()` 增加 Windows `O_BINARY`，并新增回归断言：

1. 两个 v4 sidecar 的磁盘 bytes 不得包含 CRLF 转换；
2. amendment 的 `health_v4_sha256` 必须等于 `health-v4.json` 磁盘实际 bytes 的 SHA-256；
3. create-only 行为和原 v3 摘要引用保持不变。

本地 Windows 验证：`tests/test_recorder_health_v4.py` 为 `13 passed`；完整套件收集 506 项并以 exit 0 完成（进度输出显示 1 个 skip，即 505 passed / 1 skipped）；`python -m ib_execution.provenance --check` 与 `git diff --check` 均通过。这修复未来证据发布；它不把本次既有 v4 证据自动升级为字节级完全 verified。

## 6. Owner 裁决（2026-08-20）

1. **D1 已批准：** owner 接受 SPY/RTH 的 BID_ASK、ALL_LAST 使用 30 秒 event-driven observation threshold，并批准 v4 成为 Full-RTH 最终 health authority。原 v3 FAIL 永久保留；artifact integrity、provenance 和 Gate B2 仍是独立判定层。
2. **D2 已批准：** writer lag 本轮为 `1,233.9999999967404ms <= 5,000ms`，但可复现存储 probe 未完成，因此根因保持 OPEN，不按单轮数值关闭。
3. 当前 GitHub `main` 未启用 branch protection；required CI 与独立 review 不是服务器端强制。任何保护规则变更需要单独批准。

## 7. 可能产生问题的假设与生产前强制 review

| 决定 | 依赖的风险假设 | 可能的问题 | 现有补偿控制 | 生产前必须复核 |
|---|---|---|---|---|
| D1：v4 authority + 30 秒 event-driven observation | SPY/RTH 的正常微结构可能产生数秒 BID_ASK/ALL_LAST 静默，30 秒足以区分需要升级调查的事件驱动 gap | 重复但短于 30 秒的上游停滞、两路同步静默或特定市场状态下的 feed degradation 可能不被单独升级为 hard problem；阈值可能不适用于其他资产/session | BAR_5S 15 秒 time-driven hard threshold、1100、独立 realtime-farm 状态、subscription/Recorder/raw 完整性、全链路 accounting；事件驱动 gap 仍保留为 evidence/advisory | 至少复核多个独立 Full-RTH 日和不同波动/流动性状态的 gap 分布；检查两路同步静默、接近 30 秒的尾部和 BAR/farm 交叉证据；确认 Windows v4 磁盘 bytes 摘要在 exact delivered commit/CI 中自绑定；owner 重新确认适用范围只覆盖经验证的资产/session |
| D2：writer-lag 保持 OPEN | 本轮 1,234ms 改善可能可持续，但根因未知 | 防病毒、分页、磁盘/flush stall、资源竞争或队列压力下可能再次超过 5 秒；单日低值不能证明安全裕量 | 1 秒 durability cadence 不变、bounded queue、drop/error/count mismatch 显式失败、现有 fsync/writer 指标 | 运行有界、隔离、可清理、不会制造大量废文件的 reproducible storage probe；定位或界定根因并在 exact tree 复测。若仍不能定位，必须形成新的 owner risk amendment，不能沿用 D2 |

该 review 必须在任何 order-capable Paper/Live、生产部署或最终 B2 freeze 前完成。它不允许关闭 Gateway
Read-Only、不构成订单授权，也不能用普通绿 CI 替代 Windows/存储直接证据。

## 8. 未改变事项

- `gate_b2=READ_ONLY_IN_PROGRESS`
- `order_authorization=NONE`
- `trading_adapter=NOT_IMPLEMENTED`
- 原 v3 FAIL、历史失败 artifact 和 raw 数据均未覆盖、删除或重写
