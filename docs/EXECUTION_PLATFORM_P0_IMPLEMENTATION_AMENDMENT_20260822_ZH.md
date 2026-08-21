# 执行平台 P0 实施 Amendment（2026-08-22）

状态：**LOCAL CANDIDATE IMPLEMENTED AND VERIFIED；FORMAL CAMPAIGN / EXACT-COMMIT CI / OWNER ACCEPTANCE NOT YET COMPLETED**

本文是 [`EXECUTION_PLATFORM_IMPLEMENTATION_PLAN_20260822_ZH.md`](EXECUTION_PLATFORM_IMPLEMENTATION_PLAN_20260822_ZH.md) 的实施记录。它不修改 frozen evidence，不连接 IB，不产生 broker write，也不构成 Gate B2 PASS 或订单授权。

## 1. 本候选已完成

- 新增 canonical `tree_identity`，由 provenance 与 B2 Git-object material consumer 共用；工作树和 exact Git-object bytes 使用同一选择与 hash 规则。
- 将此前被旧 `.py`-only 规则静默遗漏的两个 PowerShell 故障演练脚本纳入 source identity；未知 source suffix、遗漏 required extra、未登记 tracked config 均 fail closed。
- 保留 B2 manifest schema v2 的历史语义并显式派发；未知历史版本和尚未注册的 v3+ 均 fail closed，不修改旧 frozen manifest 迎合新规则。
- `STATE.json` 升级为 schema v4，从已提交 Git blob 验证 frozen manifest、manifest-only commit、parent/candidate 关系和 schema-v2 candidate identity。
- 新增 B2 read-only evidence candidate、freeze commit、当前代码身份匹配布尔值和逐组件 drift；没有把结构有效性、历史观测适用性和 Gate 判定压成一个 PASS。
- 新增真实 frozen manifest 回归，并保留 `REFERENCE_ONLY`、外部 bytes 未复验和 order capability 禁止边界。
- living docs 使用 `provenance-current:` 标记与机器状态双向校验，取消“已有 B1 attestation 就整体跳过”文档守卫的旧逻辑。

## 2. 当前机器派生结果

本候选重新生成 `STATE.json` 后：

```text
gate_b1_attested_freeze = e90c1f4b464c83898c036055b27e17f7eb0da0eb
gate_b1_covers_worktree = false
gate_b2_read_only_evidence_candidate = c88cf2463ba1a93940172e579575636f2778f457
gate_b2_read_only_evidence_commit = d679e879784e0bb45ff6a002d9b3a0de8f2bc66e
gate_b2_read_only_evidence_code_identity_matches_current_tree = false
gate_b2_read_only_evidence_drift_components = [source]
gate_b2 = READ_ONLY_IN_PROGRESS
order_authorization = NONE
trading_adapter = NOT_IMPLEMENTED
```

`source` drift 是从当前 canonical identity 与 frozen candidate identity 独立比较得出，不是永久回归里写死的预期。未来合法重新冻结后若 identities 相同，回归允许布尔值变为 true。

## 3. coverage 的结构性成因与 P0.5

现行 B1 coverage 规则比较 attested freeze 之后的 tracked/dirty paths。即使仅同步 living docs，也会在以下两项中选择：

1. 如实保持 `gate_b1_covers_worktree=false`，直到下一次 formal campaign；
2. 在同一 candidate 完成代码、测试和文档后，运行 formal campaign 并取得 exact-candidate owner acceptance。

本候选选择第二条作为最终收口路径，但当前尚未执行 campaign。因此此刻 coverage 必须为 false。P0 不修改 owner 已接受的 attestation 规则；living-doc 例外集合治理仍保留为独立 P0.5 PR，需独立 review、formal campaign 和 owner acceptance。

## 4. 已验证与未验证边界

已验证（本地）：P0 targeted regression；真实 frozen manifest Git blob 的 schema/ancestry/candidate identity；`STATE.json` v4 动态派生不变量；`compileall`；完整 pytest `574 passed, 1 skipped`（Windows 无 device-mapper，`test_a_delayed_volume_can_be_created_and_destroyed` 按环境条件跳过）；`python -m ib_execution.provenance --check`。完整门禁使用显式 `$LASTEXITCODE` fail-fast，未由后续成功命令掩盖失败。

尚未验证：exact-commit CI。当前工作树尚未形成可供远端 CI 绑定的 exact commit。

未执行：formal B1 freeze campaign、owner acceptance、attestation metadata commit、任何真实 IB 连接、Gateway Read-Only 切换、Paper/Live submit/modify/cancel。

## 5. 不可交换的下一步

1. 完成当前 candidate 的 targeted/full/static/provenance 验证；
2. 提交 candidate 并运行 exact-commit CI；
3. 在完全相同的 candidate 上运行 formal B1 freeze campaign；
4. owner 审查并接受 exact candidate；
5. 只提交允许的 attestation metadata；
6. attestation 后不再补代码或文档。

若任何一步改变 source/config/lock 或非允许路径，必须回到第一步，不得沿用旧 campaign。
