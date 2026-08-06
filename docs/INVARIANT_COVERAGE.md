# Invariant coverage matrix — v0.1.5.dev0

`COMPLETE` 要求三层同时存在：Property/adversarial test（P）、runtime assertion/structural enforcement（R）、offline journal auditor（A）。P/R/A 完整不等于 Gate B1 已通过；正式 campaign、进程退出集成和评审结论仍是独立退出条件。

| # | 简述 | P | R | A | 当前证据 |
|---|---|---|---|---|---|
| 1 | decision id once | yes | DB PK + atomic accept | yes | complete |
| 2 | durable before broker write | yes | commit-before-call | yes | subprocess force-kill: before/after WAL |
| 3 | one live intent per leg | yes | state gate | yes | generated restart lifecycle |
| 4 | no second send pending ACK | yes | state gate | yes | generated lifecycle |
| 5 | no replacement pending cancel | yes | state gate | yes | cancel crash window |
| 6 | write only CONNECTED+SYNCED | yes | write-boundary assertion | yes | complete |
| 7 | opening only NORMAL | yes | `_can_write` | yes | complete |
| 8 | FLATTEN_ONLY only target zero | yes | `_evaluate` | yes | complete |
| 9 | resolve working before flatten | yes | cancel/reconcile gate | yes | complete |
| 10 | restart reconcile before send | yes | forced restore before connect/reconcile | yes | real IB barrier remains Gate B2 |
| 11 | expired target never sent | yes | `_evaluate` | yes | complete |
| 12 | exec id once/corrections append-only | yes | atomic book transaction | yes | partial-fill force-kill window |
| 13 | missing fee is benign | yes | structural | yes | complete |
| 14 | unknown broker fact halts | yes | exact identity + HALT | yes | observed permId matrix remains B2 |
| 15 | explained residual -> FLATTEN_ONLY | yes | restore fold | yes | complete |
| 16 | order/share/notional/position caps | yes | RiskEngine + restart restore | yes | evidence carries frozen limits |
| 17 | intent stores config hash | yes | intent construction | yes | complete |
| 18 | callback/bridge failure fail-closed | yes | guarded callbacks + bridge liveness | yes | real adapter handlers remain B2 |
| 19 | overnight survivability | yes | numeric stress | yes | auditor recomputes per-intent evidence |
| 20 | every invariant has P/R/A | yes | coverage contract | yes | auditor fails on missing row |
| 21 | startup must-reject self-test | yes | Controller construction path | yes | matching config hash must precede start/intent |
| 22 | restart cannot clear HALT | yes | forced restore + exact CAS ack | yes | subprocess kill after durable HALT |

## Gate B1 仍未通过

正式 Hypothesis campaign 已通过：Python 3.12.13、seed `2026080601`、两个生成测试各 1,500 passing examples、source-tree SHA-256 `4990d57cddc05d21924a3b3b1d01050ecc2d9e6a8f4b36a19969b1809f0f67ba`。证据见 `artifacts/gate_b1/20260806T142435Z/manifest.json`。

截至 2026-08-06，以下任一项未完成都必须维持 `Gate B1 not passed`：

- `fatal_shutdown_requested` 已测试，但真正 execution-engine 宿主进程的退出码/监督器行为尚未集成；
- OS/卷级真实 disk-full 与 SQLite/WAL 损坏演练尚未取代当前确定性故障注入；
- 本矩阵尚需正式评审签字，不能由测试数量自动升级 Gate。

Gate B2 的 IB stable-snapshot、permId、1101/1102 和 callback observed matrix 不倒灌进 B1，也不因 B1 的 FakeBroker 证据而预判通过。
