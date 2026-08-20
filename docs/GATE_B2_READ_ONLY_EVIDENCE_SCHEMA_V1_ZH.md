# Gate B2 只读证据 schema v1

状态：**F1 IMPLEMENTED；F2 EVIDENCE COLLECTION NOT STARTED**

## 1. 权限边界

`B2_READ_ONLY_EVIDENCE` 只描述未来 exact-tree freeze 的证据包。schema 通过不等于 Gate B2 PASS，
不实现 trading adapter，也不授权 Paper/Live 下单、改单或撤单。manifest 必须原样保留：

- `gate_b2=READ_ONLY_IN_PROGRESS`；
- `order_authorization=NONE`；
- `trading_adapter=NOT_IMPLEMENTED`。

实现位于 `src/ib_execution/b2_evidence.py`；只读 CLI 为
`scripts/validate_b2_read_only_evidence.py`。该 CLI 不连接 IB、不读取账户，也不收集或复制 raw
market data。

## 2. 顶层结构

schema v1 只接受以下顶层字段，任何 unknown key 都会 fail closed：

| 字段 | 含义 |
|---|---|
| `schema_version` / `freeze_kind` | 固定为 `1` / `B2_READ_ONLY_EVIDENCE` |
| `candidate` | exact commit、tree、source/config/dependency-lock hashes |
| `safety_boundary` | 上述只读权限边界 |
| `ci_runs` | exact candidate 的 GitHub Actions run identity；只接受 `SUCCESS` |
| `evidence` | 结构化 evidence entries |
| `authority_evidence_ids` | 明确参与 freeze authority 的 bound entries |
| `required_failures` | 必须保留的历史失败；必须同时属于 authority |
| `unknowns` | 未验证、官方歧义、not-guaranteed 或 OPEN 项 |
| `risk_assumptions` | 至少包括 D1/D2 的后续 review 义务 |
| `owner_acceptance` | F2 candidate 可为 `null`；F3 final validation 必须存在 |

每个 evidence entry 必须给出逻辑 ID、独立 `claim_key`、类型、binding、受控相对路径、SHA-256、
byte size、采集 UTC、采集 commit、verdict、scope 和 sensitivity。路径只接受 normalized literal
POSIX relative path；绝对路径、`..`、反斜线、环境变量和盘符均被拒绝。

## 3. Binding 与 verdict

- `BOUND_AUTHORITY`：hash、正整数 byte size、采集 commit 都必须完整有效；只有该类型能进入
  `authority_evidence_ids`。
- `REFERENCE_ONLY`：允许保留历史索引，但永远不能被 authority list 提升成证据权威。
- `UNBOUND`：hash、bytes、capture commit 必须全部为 `null`，避免半绑定状态制造信心。
- 同一 `claim_key` 不能同时出现 `PASS` 和 `FAIL`。v3 FAIL 与 v4 health PASS 必须使用不同
  claim identity，不能覆盖或合并。
- `required_failures` 必须包含至少一个 bound `HISTORICAL_FAILURE + FAIL`；authority 必须包含至少
  一个 bound `FULL_RTH_HEALTH + PASS`。

## 4. Unknown、D1/D2 与 owner acceptance

`unknowns` 明确记录其 review point。若 `review_before=B2_FREEZE`，则
`blocks_b2_read_only_freeze` 必须为 `true`，并且任何 final owner acceptance 都会被拒绝。

D1 `D1_EVENT_DRIVEN_30S` 与 D2 `D2_WRITER_LAG_ROOT_CAUSE` 是 schema v1 的必需风险假设。两者：

- 不阻塞 B2 只读 freeze；
- 状态必须保持 `OPEN_REVIEW_REQUIRED`；
- 必须在任何生产或 order-capable Paper/Live 前重新 review。

F3 owner acceptance 只能使用 `ACCEPT_EVIDENCE_SCOPE_AND_RESIDUAL_RISK`，并逐项接受 manifest 中
完整的 authority/unknown/risk ID 集合。它必须同时声明
`REMAIN_READ_ONLY_IN_PROGRESS + NOT_AUTHORIZED`；任何 Gate PASS 或订单能力表述都会被拒绝。

## 5. 验证层级

F1 validator 只证明 schema 与跨字段语义一致，不证明文件真的存在、磁盘 bytes 与摘要一致、
capture commit 可由 Git 解析，或 CI run 可由 GitHub 复核。以上属于 F2 builder 的职责；F3 还必须
从 Git objects 验证 candidate 与 metadata-only 边界。

候选包：

```powershell
.\.venv312\python.exe scripts\validate_b2_read_only_evidence.py <manifest.json>
```

最终 owner-accepted 包：

```powershell
.\.venv312\python.exe scripts\validate_b2_read_only_evidence.py <manifest.json> --require-owner-acceptance
```

CLI 的 `PASS` 仅表示 `valid B2_READ_ONLY_EVIDENCE structure`，明确不是 Gate B2 PASS 或订单授权。
