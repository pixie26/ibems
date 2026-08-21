# 执行平台优化实施计划（2026-08-22）

状态：**APPROVED PLAN；P0 LOCAL CANDIDATE IMPLEMENTED，LOCAL VERIFICATION PASS**

实施记录见 [`EXECUTION_PLATFORM_P0_IMPLEMENTATION_AMENDMENT_20260822_ZH.md`](EXECUTION_PLATFORM_P0_IMPLEMENTATION_AMENDMENT_20260822_ZH.md)。该记录不构成 formal campaign、owner acceptance、Gate B2 PASS 或订单授权。

本文记录 2026-08-22 对执行平台后续工作的最终排序和验收边界。它是实施决策记录，不是 Gate authority、B1/B2 attestation、订单授权或历史证据的替代品。

当前机器状态只以仓库根目录 `STATE.json` 的重新派生结果为准。本文不得被解释为：

- Gate B2 已通过；
- Paper/Live 已获得下单或撤单授权；
- B2 只读历史观测天然适用于任意后续代码树；
- Gate A 的外部策略结论已被本仓库独立验证；
- 普通 CI 可以替代正式 freeze campaign、owner acceptance 或真实 Gateway 观测。

## 1. 已接受的判断

1. Gate 顺序保持不变，不得用后续阶段证据倒推前序 Gate。
2. B1 exact freeze 与 B2 read-only evidence freeze 是不同 authority；B2 freeze 绝不映射为 `Gate B2 PASS`。
3. 代码身份覆盖和历史观测适用性必须分开表达，不能聚合成一个容易误读的布尔值。
4. 文档漂移不是单纯纪律问题，而是当前 diff-based B1 coverage 规则制造的结构性激励；P0 记录并暴露问题，规则修改单列 P0.5。
5. Gate A 是 owner 对外部策略证据的 attested claim，不是本仓库可自行验证的事实；缺失、损坏或不可解析时必须 fail closed。
6. 任何 order-capable Paper/Live 工作之前，必须完成 D1/D2 review、Read-Only 安全边界切换记录、execution-quality 口径冻结和单次 owner 授权。
7. “平台更难错误地下单”和“成交质量改善”是两类独立结论。B1/B2 的安全改进不能被描述为成本或成交质量改善。

## 2. 已核实的基线事实

以下事实绑定于本文形成时的 Git 基线，不替代运行时重新派生：

- B1 attested freeze：`e90c1f4b464c83898c036055b27e17f7eb0da0eb`。
- 本文形成前的 `origin/main` / 当前基线：`9336dbc`。
- B2 read-only candidate：`c88cf2463ba1a93940172e579575636f2778f457`。
- frozen manifest commit：`d679e879784e0bb45ff6a002d9b3a0de8f2bc66e`；其 parent 正是上述 B2 candidate，且该提交只增加 frozen manifest。
- frozen manifest 的 candidate source hash 是 `4a4643b93dcb691c9214911c8f7789be3abaa5a2b2070c588952afc78d025f34`。
- 本文形成前基线的 source hash 是 `fa255c266f29c1704c30140313119fa051a1d622b74fa1502b8f8576d7ee3189`。
- 当前 `gate_b2` 仍是静态状态字符串；frozen manifest 尚未作为真实提交对象进入回归消费。
- 当前 B1 coverage 是 diff-based：attestation freeze 之后任何不在允许集合内的 tracked 或 dirty path 都会使 coverage 失效；它不只比较 source/config/lock hash。
- `provenance.py` 与 B2 evidence material 路径对 source/config identity 的定义存在重复真相源。
- 当前文档守卫在已有 B1 attestation 时提前返回，无法双向捕获 living docs 的过期或过度保守状态。

## 3. P0 — 状态一致性与可回归 provenance

范围：同一个候选变更完成代码、测试、living docs 和 dated amendment；不实现 broker write，不连接 IB，不改变订单授权。

### P0-A — 统一 tree identity 定义

建立一个 canonical tree-identity 模块，集中定义并导出：

- source roots、suffixes 和 extra files；
- config roots/globs；
- dependency lock identity；
- 从工作树和 Git object 重算 identity 所需的纯函数。

`provenance` 与 B2 evidence material 必须消费同一组常量和规则。测试必须证明：

1. 两个消费者使用相同 canonical 定义；
2. working-tree 与 exact Git-object 路径在同一内容上得出相同 identity；
3. fixture 测试之外，B2 candidate derivation 与 provenance derivation 存在直接交叉断言；
4. 未知文件类型、遗漏 extra file 或 config glob 漂移不会被静默忽略。

### P0-B — 版本化 B2 manifest validator

保留 schema v2 的历史验证语义，新增显式版本派发，而不是修改旧 frozen manifest 迎合新 validator：

```text
validate_manifest_v2(...)
validate_manifest_v3_or_later(...)
validate_manifest(...) -> dispatch by schema_version
```

历史 manifest 必须从其已提交 Git blob 读取和验证；validator 的新版本不得让旧证据静默失效，也不得通过放宽条件让不合格证据变绿。

### P0-C — STATE schema v4 的派生字段

`STATE.json` 只能由 provenance 生成。新增字段的建议语义：

```text
gate_b2_read_only_evidence_candidate
gate_b2_read_only_evidence_commit
gate_b2_read_only_evidence_code_identity_matches_current_tree
gate_b2_read_only_evidence_drift_components
```

约束：

- `gate_b2` 保持 `READ_ONLY_IN_PROGRESS`；
- `order_authorization` 保持 `NONE`；
- `trading_adapter` 保持 `NOT_IMPLEMENTED`；
- coverage 布尔只表达 candidate code identity 是否等于当前树，不表达历史观测是否适用；
- 每项 evidence 保留自己的 `collected_at_commit`、采集时间、范围和边界；
- 不得把 historical evidence validity、code identity match 和 observation applicability 压成一个总 PASS。

### P0-D — 动态派生与不变量回归

回归测试不得硬编码“当前 coverage=false”或“当前仅 source drift”。必须由独立路径重算后断言：

```text
derived value == independently recomputed value
drift_components is empty <=> code_identity_matches_current_tree is true
code_identity_matches_current_tree is true
    => source/config/lock identities all match
```

“本文实施时当前为 false / source drift”只进入一次性验收记录和 dated amendment，不进入永久回归断言。这样未来合法重新冻结并得到 `true` 时，测试不会反对正确状态。

独立重算不得复用被测函数的中间结果，否则不是有效的交叉证明。

### P0-E — frozen manifest 进入真实回归

新增测试必须：

1. 从 Git object 读取 `docs/GATE_B2_READ_ONLY_EVIDENCE_C88CF246_FROZEN.json`；
2. 通过对应 schema 版本 validator；
3. 验证 frozen manifest commit 的 parent/candidate 关系；
4. 验证 manifest candidate commit、tree、source/config/lock identity；
5. 验证派生 STATE 字段与独立重算一致；
6. 保持外部 artifact 的 `VERIFIED / PARTIALLY_VERIFIED / UNKNOWN / REFERENCE_ONLY` 边界，不把结构校验冒充外部 bytes 复验。

### P0-F — 文档同步与守卫升级

同一候选内更新 living docs：

- `README.md`；
- `docs/IMPLEMENTATION_STATUS.md`；
- `docs/GATE_B2_STATUS_20260810_ZH.md` 的后续 amendment 区域。

旧的 FAIL、旧 freeze 和历史判断保持原意。`docs/FINAL_EXECUTION_PLAN_ZH.md` 作为 dated/frozen 文档不直接重写正文，只允许增加清晰 amendment/supersession 指针。

文档守卫取消“已有 B1 attestation 即整体 return”的逻辑，并双向检查：

- living docs 的当前状态引用必须来自机器 authority，不能手工维护另一份真相；
- 需要出现的 40-hex attested freeze 引用必须等于当前派生值；
- 历史 freeze、事故、review 和 evidence 文档通过明确白名单或窄化的 `provenance-allow:` 语义保留；
- “当前值”与“历史值”必须可机器区分，不能靠读者猜测上下文。

### P0-G — 记录 diff-based coverage 的结构性成因

在 dated amendment/决策记录中明确：当前 B1 attestation 规则使任何 living-doc 变更都要求在以下两种结果中选择：

1. 接受 `gate_b1_covers_worktree=false` 直到下一次正式 campaign；
2. 为文档同步运行一次正式 campaign。

P0 只把问题机器化暴露并给出 P0.5 提案，不在同一 PR 内修改 owner 已接受的 attestation 规则。

### P0-H — 候选、campaign 与 attestation 顺序

顺序不可交换：

1. 在同一个 candidate 中完成 P0 的代码、测试、living docs 和 dated amendment；
2. 运行 targeted tests、全套测试、provenance check 和 exact-commit CI；
3. 在该 candidate 上运行正式 B1 freeze campaign；
4. owner 对 exact candidate 做新的 acceptance；
5. 只提交允许的 attestation metadata；
6. attestation 之后不得再补文档或其他 tracked 修改。

P0 最终期望状态：

```text
Gate B1 historical attestation       valid
Gate B1 covers current worktree      true, after the new campaign
B2 frozen historical manifest        valid under its schema
B2 candidate code identity match     independently derived
Gate B2                              READ_ONLY_IN_PROGRESS
order_authorization                  NONE
broker writes                        0
```

### P0 验收命令

具体测试节点以届时 `.github/workflows/ci.yml` 为准，最低要求：

```text
pytest -k "provenance or b2_evidence or attestation"
pytest
python -m compileall src tests scripts
python -m ib_execution.provenance --check
exact-commit CI
formal B1 freeze campaign
```

必须确认旧 B1 historical validation、`gate_b1_covers_worktree` 派生和 attestation metadata-only 约束没有退化。

## 4. P0.5 — living docs attestation 规则治理（独立 PR）

这是 owner acceptance 规则变更，不得塞进 P0 bug-fix PR。建议把当前单一 allowed set 拆成：

```text
ATTESTATION_METADATA_PATHS
LIVING_STATUS_DOCS
FROZEN_OR_EVIDENCE_PATHS
```

建议语义：

- committed post-attestation diff 可允许 `ATTESTATION_METADATA_PATHS | LIVING_STATUS_DOCS`；
- dirty worktree 只允许 `ATTESTATION_METADATA_PATHS`，living docs 不能以未提交状态被 coverage 忽略；
- `LIVING_STATUS_DOCS` 必须与 source/config/lock identity 输入完全不相交；
- `LIVING_STATUS_DOCS` 必须与 frozen spec、ADR、signoff/evidence、freeze plan、dated incident/review 文档完全不相交；
- attestation 与 docs guard 必须导入同一个 canonical 常量，不能复制两份白名单；
- 集合互斥、路径存在性、大小写和 rename 行为必须有测试锁定。

收益：living status 可随机器状态同步，不再为纯导航/状态说明消耗正式 campaign，降低文档长期过期的激励。

代价：owner acceptance 的周边叙述可在不重新签字时变化。只有当不可变 signoff/evidence 继续从 Git object 读取、行为 identity 不受影响、living docs 边界严格互斥时，该代价才可接受。

P0.5 自身修改 attestation 规则，因此必须独立 review、正式 campaign 和 owner acceptance。

## 5. P1 — Gate A 治理分叉

工程在进入 order-capable adapter 前等待机器可读 Gate A 决策。建议新增有 schema 的 `GATE_A_VERDICT.json`，至少包括：

```text
verdict
attested_at
strategy_repository
strategy_commit
second_consumer
evidence_summary_path
evidence_sha256
owner_attestation
```

语义要求：

- `strategy_repository` / `strategy_commit` 是本仓库外部对象；其结论只能标记为 owner-attested external claim；
- `evidence_sha256` 必须绑定已提交、有界、可复查的摘要，不能只绑定外部可变链接；
- 缺失文件、未知字段、非法枚举、摘要不匹配或不可解析一律 fail closed；
- fail-closed 的默认投资决策是 `STOP_TRADING_ADAPTER_INVESTMENT`，绝不能把“没有 verdict”解释为“不受阻塞”。

决策分叉：

```text
NO_GO or INSUFFICIENT_EVIDENCE
and SECOND_CONSUMER == NONE_CONFIRMED
    -> 停止交易型 Adapter 投入
    -> 保留 Recorder / journal / FakeBroker / 风险核心
    -> 资源转向数据验证与 Gate C

GO_TO_DATA_VALIDATION
    -> 才允许进入 P2
```

## 6. P2 — 仅当 Gate A 允许时进入 B2 order-capable 工作

### B2-a — IB 协议离线化

先在 FakeBroker/fixture 上实现 IB 特有语义测试，不连接 IB：

- error code 分类；
- uncertain send；
- callbacks 乱序、重复和迟到；
- `permId` 出现时机与跨 session identity；
- commission/fee callback 迟到；
- cancel-reject 非终态；
- partial-fill/cancel race；
- correction 与 replay；
- stable snapshot barrier。

同时在 B2-c 前冻结 execution-quality schema：

- arrival price 定义；
- wall/monotonic/broker timestamp 的权威边界；
- decision/send/ack/fill latency 分解；
- partial-fill 归并规则；
- reject/miss reason taxonomy；
- future 1s/3s/5s adverse move 的采样语义。

### B2-b — 最小 order adapter，默认不可用

实现最小 adapter，但保持默认禁用，并要求 platform gate 与配置双开关。每次 `place_order` 前仍必须 durable intent-before-send。

capability 约束：

- capability 不得仅由 config 构造；
- 必须消费 owner-signed、限定 account/environment/instrument/quantity/time-window/clientId/orderRef prefix 的授权对象；
- 无 capability、过期 capability、范围不匹配或签名/摘要失败必须在 adapter 边界拒绝；
- spy broker 统计 attempted write calls，而不是只统计成功调用；无 capability 时 attempted broker write count 必须为 0；
- submit、cancel 和后续独立实验应各自受明确 capability scope 约束。

该阶段改变安全关键代码，完成后必须重新运行 B1 formal freeze，不得沿用旧 B1 coverage 宣称当前树已获 attestation。

### B2-c0 — order-capable 前置安全边界

在任何真实 Paper broker write 前，依次完成：

1. 收口 frozen manifest 中 D1/D2 的 required review；
2. 冻结 execution-quality schema；
3. 记录关闭 Gateway Read-Only 保护的 owner 决策；
4. 切换前做完整 snapshot 和 preflight；
5. 获取单次 owner 授权：Paper、SPY、1 股、marketable limit、明确时间窗、明确 clientId/orderRef prefix；
6. 实验后立即恢复 Read-Only，并再次做 snapshot/preflight；
7. 使用非破坏性的 positive control 确认 Gateway 接受恢复：Read-Only 开启时 completed-orders 触发已知提示，且 10 秒内没有 completion；
8. 禁止通过“试着发一单看是否被拒”验证 Read-Only 恢复。

Read-Only 切换是安全边界反向切换，必须作为独立 owner decision/evidence item，不能隐藏在“B2 下单子阶段”标题中顺带执行。

### B2-c1 / B2-c2 — 分离实验授权

第一窗口只做：

```text
single submit -> fill -> reconcile
```

通过并完成证据复核后，另开授权窗口做：

```text
cancel -> reject/fill race -> reconcile
```

不得用一个宽泛授权覆盖多种风险不同的 broker write 场景。

### B2-d — append-only evidence

每轮结束立即写 documented-vs-observed 增量和新的 evidence manifest。旧的 `C88CF246` frozen manifest 永不修改；后续结论使用新版本、amendment 和独立 hash 追加。

## 7. P3 及以后

顺序保持：

1. Gate B3 真实订单态故障演练：1101、订单在途 Gateway restart、cancel/fill race、迟到 fee/correction、stale snapshot 等；现有 FakeBroker 强杀或空状态 restart 不能替代。
2. Gate C L1/L2/L3：live/replay 确定性、IB 与 research parquet 语义差异、历史与 live raw-feed 数据源敏感性；参数不得重调。
3. 只有 A、B1、B2、B3、C 全部通过，才进入 Gate D 的独占账户 1 股 live 成本实验。
4. Gate E 独立做费用、微量实测成本和容量模型的资金决策；5 股数据不能证明 market impact、queue depletion 或 auction capacity。

尾部成本继续使用不对称更新：实测坏于 stress 时立即上调或停止；实测好于 stress 时不立即下调，直到达到预注册 tail N 和区间宽度。

## 8. master 集成约束

不能把“只新增本计划文档”直接合入 `master`，同时继续声称现有 B1 attestation 覆盖当前树。原因是当前 coverage 规则按 freeze 后的 tracked diff 判定，任何未获允许的 Markdown 提交都会使 `gate_b1_covers_worktree=false`。

因此本文件应采用以下集成方式之一：

1. **推荐：** 与 P0 的代码、测试和 living-doc 修复一起进入同一个 candidate，随后完成 exact-commit CI、正式 B1 campaign、owner acceptance 和 metadata-only attestation，再合入 `master`；
2. 若 owner 明确要求先单独合入本文，则必须同步重新生成机器状态并如实接受 `gate_b1_covers_worktree=false`，直到后续正式 campaign；不得保留过期的 `true`。

任何向 `master` 的合并/推送仍需遵守 protected-branch 流程。普通 feature-branch CI、本文的 APPROVED PLAN 状态或历史 B1 PASS 都不构成更新受保护分支的执行授权。

## 9. 明确不做

本计划本身不授权：

- 修改或删除既有 frozen spec/evidence；
- 为赶进度删除安全检查；
- 连接 IB 或关闭 Gateway Read-Only；
- Paper/Live submit、modify 或 cancel；
- 自动 watchdog 重启、下单或改 mode；
- 将 B2 read-only freeze 政名或映射为 Gate PASS；
- 在 Gate A 分叉前继续投入交易型 adapter；
- 用普通 CI 或历史观测替代当前 exact-tree evidence。
