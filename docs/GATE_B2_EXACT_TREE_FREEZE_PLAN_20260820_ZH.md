# Gate B2 只读 exact-tree freeze 实施计划（2026-08-20）

状态：**PLAN READY；FREEZE NOT YET EXECUTED**

## 1. 目的与非目标

本计划把当前 B2 只读阶段的 source、tests、docs 和 evidence index 绑定到一个可复查 Git exact tree，解决“历史证据存在，但当前树没有对应 B2 freeze authority”的问题。

这次 freeze **不是**：

- `Gate B2 PASS`；
- paper/live order authorization；
- 对非空 reconciliation、订单身份、跨 client 可见性或订单生命周期的证明；
- 对 D1（30 秒 event-driven observation threshold）或 D2（writer-lag 根因）的最终生产审查；
- 把未追踪的大型 raw market-data 文件提交进 Git。

freeze 完成后，`STATE.json` 仍应保持 `gate_b2=READ_ONLY_IN_PROGRESS`、`order_authorization=NONE`、`trading_adapter=NOT_IMPLEMENTED`，除非未来另有独立、经过审查的机器状态设计和 owner 授权。

## 2. Freeze contract

一个可接受的 B2 read-only freeze 必须同时绑定：

1. candidate Git commit 与 tree hash；
2. `STATE.json` 的 source/config/dependency-lock hashes；
3. exact commit 上的普通 CI 结论；
4. Full-RTH v4 health authority、历史 v3 FAIL 和事故证据的不可混淆索引；
5. Windows Task Scheduler lifecycle probe 的 report SHA-256、大小、测试树和清理 `0/0`；
6. 每项外部 evidence 的逻辑名称、相对/受控路径、SHA-256、byte size、采集时间、采集 commit、结论与边界；
7. 官方 documented-vs-observed 复核版本，以及其中的 official ambiguity / not-guaranteed 项；
8. 未验证、被策略阻断和明确延后的项目；
9. D1/D2 作为后续 risk assumptions 的登记，并明确“不阻塞本次只读 freeze，但阻止未经 review 的生产或 order-capable Paper/Live”。

任何缺少 hash、大小、采集树或 verdict 边界的 artifact，只能列为 `UNBOUND` 或 `REFERENCE_ONLY`，不能进入 freeze authority。

## 3. Evidence 存储与大小策略

- Git 只提交小型、脱敏、可审查的 manifest/摘要/报告；不提交账户标识、余额、授权信息、原始 API log 或全日 tick/raw/parquet。
- 大型 raw evidence 保持在既有受控本地 evidence root；manifest 只记录相对路径、SHA-256、byte size、row/count 摘要和保留策略。
- 历史失败证据不能删除或覆盖；它与后续 PASS authority 分别列项。
- 不为 freeze 重新制造大型文件，不运行 disk-full、fault injection 或新的 IB 实验。
- 验证脚本使用 streaming hash 和 metadata read，不把大文件整体读入内存，也不复制 evidence。
- 路径必须经过 allowlist/root resolution；manifest 不接受 `..`、任意绝对路径或无法解析的环境变量。

## 4. 分阶段实施

### F0：当前树收口

1. 完成官方 IB 文档逐项复核，消除旧待复核标记。
2. 更新 living status/README，明确 v4 health PASS 与 Gate B2 非 PASS 的区别。
3. 在本地运行受影响测试、完整普通 regression（若成本可接受）与 `python -m ib_execution.provenance --check`。
4. 提交并让 exact commit CI 通过。

退出条件：文档一致、工作树干净、CI 绑定 exact commit、没有状态升级。

### F1：定义机器可验证的 B2 evidence schema

新增独立于 B1 attestation 的 B2 read-only schema，至少包含：

- `schema_version`、`freeze_kind=B2_READ_ONLY_EVIDENCE`；
- candidate commit/tree 与 source/config/lock hashes；
- structured evidence entries（hash、bytes、capture commit、verdict、scope、sensitivity）；
- required failures/unknowns/risk assumptions；
- exact CI run identity；
- owner acceptance 仅接受“证据范围与残余风险”，不接受订单能力。

schema 校验必须拒绝 unknown keys、重复 evidence id、零/非法大小、无效 hash、路径逃逸、PASS/FAIL 冲突、把 `REFERENCE_ONLY` 当 authority，以及缺少历史 FAIL 的情况。

退出条件：schema、builder/validator 和回归测试经过独立 review；失败样例能证明旧的宽松行为会被拒绝。

### F2：生成 candidate evidence manifest

1. 从 Git 对象读取 candidate tree 内的 source/tests/docs，不从脏工作树猜测历史事实。
2. 对外部小型 sidecar 重新计算实际磁盘 bytes 的 hash/size；不得信任预写入摘要。
3. 对大型 raw 只做 streaming verification 和 manifest 引用，不复制进 Git。
4. 把 v3 FAIL、v4 authority、事故、scheduler lifecycle、CI 和官方复核分别列项。
5. 生成可重放验证报告；任何缺失项 fail closed。

退出条件：manifest validator PASS；敏感字段扫描 PASS；Git 新增 evidence 大小在审查预算内。

### F3：exact-tree freeze

采用两阶段、不可自指的交付：

1. `candidate` commit 固定 source/tests/docs/schema；在 exact candidate 上运行 CI 和 evidence builder。
2. `freeze metadata` commit 只允许加入最终 manifest、owner scope acceptance 和由工具生成的状态/索引；diff 必须是 metadata-only。
3. validator 从 Git objects 复核 candidate 与 metadata，不依赖当前工作树覆盖历史 bytes。
4. exact metadata commit 再跑 ordinary CI/validator；Draft PR 保持 Draft，直到 reviewer 验证 contract。

若机器状态仍没有正式的 B2 read-only freeze 字段，本阶段只产出 `B2_READ_ONLY_EVIDENCE_FROZEN` artifact，不修改现有 `gate_b2` 枚举。禁止把文档里的“freeze”映射成 Gate PASS。

## 5. 阻断条件

以下任一项阻止 freeze：

- candidate commit/tree/source/config/lock 任一无法精确解析；
- exact-commit CI 非 green 或 job 实际未执行；
- evidence hash/size 与磁盘 bytes 不符；
- v3 FAIL 被删除、覆盖或与 v4 authority 混为一个 verdict；
- artifact 含账户标识、敏感 API log、授权信息或未经批准的大型 raw；
- 官方 executions window 歧义被静默选边；
- snapshot end callbacks 被表述为官方 atomic/stable barrier；
- `STATE.json` 被手改，或 freeze 被写成订单授权/Gate PASS；
- unrelated worktree changes 混入 metadata-only commit。

D1/D2 不在上述 freeze blockers 中；它们保留为生产/order-capable 前的 mandatory review。

## 6. 当前进度与下一动作

- F0 官方复核与文档收口：已完成；官方矩阵、living status、README 和本 contract 已交付，后续 CI #97 暴露的 Windows post-exit lock-release 测试边界也已在 exact code commit `6424190` 修正，并由 CI #98 / `b1-storage-fsync` #66 验证。
- F1-F3：尚未实现。仓库当前只有 B1 attestation/finalize 工具，没有 B2 read-only evidence schema 或 machine validator。
- 本次提交完成 F0 后，下一项应是对 F1 schema 做代码级设计审查；审查通过后才实现 builder/validator。不能仅凭一份 Markdown plan 宣称 exact-tree freeze 已完成。
