# Gate B2 只读证据 schema v2

状态：**IMPLEMENTED；candidate-v3 与 F3 尚未执行**

v2 延续 [v1](GATE_B2_READ_ONLY_EVIDENCE_SCHEMA_V1_ZH.md) 的全部安全边界，并关闭
candidate-v2 独立审查发现的三项 provenance 缺口。它仍只描述 B2 read-only evidence，绝不表示
Gate B2 PASS、生产部署、order-capable Paper/Live 或任何订单授权。

## 1. v2 新增的强制绑定

每个 manifest 必须包含 `ci_artifacts`。每项 artifact binding 精确记录：

- GitHub run ID、job name、artifact ID 与 artifact name；
- GitHub API 发布的 archive SHA-256；
- archive 内 checkout identity member；
- 如 artifact 承载 BOUND `CI_ARTIFACT` evidence，则同时记录 evidence ID 与 member path。

每个被 GitHub 报告为成功且实际执行的 job 都必须恰好有一个 artifact binding。artifact name 必须包含
candidate commit；archive 内 identity 必须精确匹配 candidate commit/tree、repository、workflow、run、
attempt 和 job。任何遗漏、重复、过期 artifact、digest/size 不一致、unsafe ZIP path、identity 不一致或
sidecar member bytes 不一致均 fail closed。

## 2. exact checkout

普通 `ci` 与 `b1-storage-fsync` workflow 在 PR 上均显式 checkout
`${{ github.event.pull_request.head.sha || github.sha }}`。每个 CI job 通过
`scripts/write_ci_checkout_identity.py` 把实际 `git rev-parse HEAD`、tree hash 与 GitHub run identity 写入
其上传 artifact。F2 verifier 下载受审 archive 后重新计算 digest，并验证该 identity；不再把 API
`head_sha` 单独当作实际 checkout 的证明。

## 3. repo evidence

`repo/...` evidence 的 bytes 必须从 `capture_commit:path` Git object 读取并计算 hash/size。工作树只用于
运行工具，不再拥有这些 evidence bytes；dirty、CRLF checkout 或未追踪替换文件不能生成新的 BOUND
authority。外部受控 evidence 仍按 allowlisted root streaming hash，不复制大型 raw 文件。

## 4. 保持不变的边界

- `gate_b2=READ_ONLY_IN_PROGRESS`；
- `order_authorization=NONE`；
- `trading_adapter=NOT_IMPLEMENTED`；
- v3 FAIL、事故 evidence、unknowns 和 scope limitations 不得删除或升级；
- D1/D2 保持 `OPEN_REVIEW_REQUIRED`，不阻塞本次 read-only freeze，但阻止未经复核的生产或
  order-capable Paper/Live；
- owner acceptance 仍只能接受 evidence scope 与残余风险，不能授权订单。
