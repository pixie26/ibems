# Full-RTH 验收报告复核（2026-08-18 运行 / 2026-08-19 复核）

## 0. 本文定位

本文复核 2026-08-18 会话提交的 Full-RTH 验收报告（Task Scheduler 独立托管，结论 **PASS**）。

本文**不是** Gate 判定，也**不**把 `docs/GATE_B2_STATUS_20260810_ZH.md` 的
「SPY Full-RTH 全日 health = 失败，未完成」升级为 PASS。原因见 §3：本次运行的全部原始证据位于
Windows 主机的 `artifacts/`（仓库 `.gitignore` 排除），复核方无法在仓库内独立验证，且**运行所用代码尚未进入 `main`**。

本文记录：仓库侧可独立复核的部分、必须由 Windows 主机补齐的绑定、复核中发现的三处需要修正或裁决的点，以及按原计划的下一步。

## 1. 最重要的一条结论：运行树不是 `main`

报告依赖 `health-v4.json`、`manifest-amendment-v4.json` 与 v4 重分析管线。核对结果：

| 文件 | `main` (`cbd1289`) | `agent/repo-hygiene-safe` (`17b3375`) |
|---|---|---|
| `src/ib_execution/recorder_health_v4.py` | 不存在 | 存在 |
| `scripts/reanalyze_full_rth_v4.py` | 不存在 | 存在 |
| `scripts/run_full_rth_recorder_task.py`（同进程 task host） | 不存在 | 存在 |
| `scripts/validate_full_rth_finalize_replay.py` | 不存在 | 存在 |
| `scripts/verify_windows_full_rth_task_lifecycle.py` | 不存在 | 存在 |

即：报告描述的运行不可能来自 `main`。它只能来自 `agent/repo-hygiene-safe`（`17b3375`）或
`agent/full-rth-v4-repair`（`34f3ac4`）。这两条分支共享 27 个 commit，之后各自分叉，**因此二者互不等价**：

- `agent/full-rth-v4-repair` 独有 `34f3ac4`（新增 `CLAUDE.md`，内容为一行 `@AGENTS.md`）
- `agent/repo-hygiene-safe` 独有 `a253a4a`（`.gitignore` 加固）与 `17b3375`（根 `FINAL_EXECUTION_PLAN_ZH.md` 与当前 Gate 状态分离）

在 `task-launch.json` 记录的 exact commit 被读出来以前，本次运行**未绑定到任何一棵可复查的树**。
按 `AGENTS.md`「Bind every safety claim to exact source, configuration, dependencies, resolved environment, and raw artifacts」，
这是本次验收当前最大的未闭合项——比报告自陈的内存未测量更关键。

## 2. 仓库侧可独立复核并通过的部分

以下项目在本仓库内复核，结论为 **verified**：

1. **报告内部账目自洽。** `3,520,043 + 532,628 + 4,680 = 4,057,351` 与 `handled = selected` 相等；
   `4,057,351 + 399 SYSTEM = 4,057,750` 与 `enqueued = persisted = readback = accepted = parquet_rows_verified` 相等。
2. **BAR_5S 全日无缺。** `390 分钟 × 12 根/分 = 4,680`，与报告一致。
3. **RTH 窗口正确。** 首条 `13:30:05Z`、末条 `19:59:59Z`，落在 `13:30Z–20:00Z` 正式 RTH 内。
4. **finalize 耗时外推自洽。** Aug-14 replay `116.234s / 2,645,388 行`，按行数比例外推到 `4,057,750 行` 得 `178.3s`；
   本轮 v3 管线 `204.5s`，即 `+14.7%`，与报告 `+15%` 一致。
5. **v4 确实决定退出码。** `scripts/run_full_rth_recorder_task.py` 在 finalize 之后调用 `write_reanalysis_v4()`，
   读回 `health-v4.json` 的 `health_ok` 并据此返回 `EXIT_HEALTH_PASS(0)` / `EXIT_HEALTH_FAIL(2)`，
   同时把 v3 结论单独记为 `health_v3_ok`。报告「exit code 锚定 v4」属实，且是代码的既定行为，不是事后解释。
6. **`repo-hygiene-safe` 树本身健康。** 在该树上运行完整测试套件：`502 passed, 4 skipped`（Linux）；
   `python -m ib_execution.provenance --check` 返回 `STATE.json matches the worktree and derived gate attestation`。

## 3. 只能由 Windows 主机补齐的项（当前 not verified）

以下内容在本仓库内不可见，必须从运行主机取回并登记 SHA-256：

1. `artifacts/ib_preflight/20260817_full_rth_task/task-launch.json` 与 `launch-decision-note.md` 中的
   **exact commit、`STATE.json` 的 source/config/lock hashes、client id**。
2. `raw/2026-08-18/` 下的 `health.json`、`health-v4.json`、`manifest.json`、`manifest-amendment-v4.json`、`events.parquet` 的 digest。
3. `task-runtime-status.json` 的 phase 序列与 `recorder-stderr.log` 的零字节事实。
4. **峰值内存**——报告已诚实声明本轮未直接测量，是唯一自陈的 partially verified 项。
   基线为 Aug-14 replay 的 `458,805,248 bytes` working set / `2,645,388` 行。

## 4. 复核发现的三点

### 4.1 watchdog 余量算错一小时（方向保守，但需更正）

代码事实（`scripts/run_full_rth_recorder_task.py`）：
`deadline = session.end + FINALIZE_GRACE(3h) + DEADLINE_SAFETY(30m)`，`deadline_rule="RTH_CLOSE_PLUS_3H_FINALIZE_PLUS_30M_SAFETY"`。

RTH close `20:00Z` → deadline `23:30Z` = `07:30 HKT`。FINALIZED 于 `04:05:40 HKT` = `20:05:40Z`。
实际余量为 **3h24m20s**，报告写作 `2h24m`。

差值正好一小时，方向上**低估**了安全余量，因此不会抬高 PASS 结论；但按本仓库的取证标准应更正后再入档。

### 4.2 v3 `health_ok=false` 与 §6.7 判定条件的冲突需要 owner 裁决

`docs/GATE_B2_STATUS_20260810_ZH.md` §6.7 写的 PASS 条件是「全日覆盖、**`health_ok=true`**、……」。
该条写于 v4 存在之前，因此「`health_ok`」当时唯一指向 v3。

本轮事实：v3 `health_ok=false`（`BID_ASK: 5 gaps over 5s`，legacy 5s 阈值），v4 `health_ok=true`（30s 观察阈值），
进程退出码取 v4。报告另行独立复算得 4 个 ≥5.0s 的 gap（最差 5.1155s，与 v3/v4 完全一致，仅边界差一个），
全部集中在 `16:19–16:22Z`，同窗口 BAR_5S 72 根正常轮转（最大间隔 5.99s）。

工程判断上，报告的解释（美东午盘流动性低谷的自然微结构，非馈送或写路径停滞）是有说服力的，且
`RECORDER_STORAGE_AND_WINDOWS_POLICY_ZH.md` 已明确「原 v3 FAIL 永久保持原结论」、v4 为 create-only amendment。

但**「§6.7 的 `health_ok` 从此读作 v4」是一次判定权移交，属于 owner 决定，不能由工程推导或退出码自动产生。**
建议按 §3.1/§3.2/§3.3/§3.4 的同样格式，在 `GATE_B2_STATUS_20260810_ZH.md` 新增 §3.5 记录该裁决，
并同时明确：v4 把 BID_ASK/ALL_LAST 事件驱动流的失败阈值从 5s 放宽到 30s 观察阈值，是否为该资产/该 session 的正确阈值。

在该裁决落纸以前，本轮严格来说是「v4 PASS、v3 FAIL」，不是无条件 PASS。

**2026-08-20 owner 决定补记：** owner 已接受 SPY/RTH 的 BID_ASK、ALL_LAST 使用 30 秒
event-driven observation threshold，并批准 D1：§6.7 的最终 Full-RTH `health_ok` authority 从 v3 移交给 v4，
见 `GATE_B2_STATUS_20260810_ZH.md` §3.5。原 v3 FAIL 永久保留；该决定不修复 artifact/provenance 缺口，
也不把本轮自动升级为 Gate B2 PASS。D1 已关闭 owner 判定权问题，但其市场微结构假设必须在生产前重审。

### 4.3 `max_writer_lag_ms` OPEN 项的关闭条件只满足了一半

`RECORDER_STORAGE_AND_WINDOWS_POLICY_ZH.md` 的 OPEN 项原文要求：
「在不改变 1 秒 durability cadence 前，**需用可复现存储 probe 定位**并以 `max_writer_lag_ms <= 5000` 关闭。」

本轮实测 `1,234ms`，满足数值条件；但**未做可复现存储 probe，成因从未定位**。
本轮同时更换了宿主（Task Scheduler 直接拥有 Python，不再经 Codex/AppX）并重写了 finalize 路径，
因此改善**似乎**可归因，但归因未被证明。

两个可选处置，任选其一即可，但必须显式写下来：
- (a) 修订 OPEN 项的关闭条件，声明「单轮全日实测 `<= 5000ms` 即可关闭」，并记录成因未定位；
- (b) 保留 OPEN 项，标注「数值条件已满足，待 probe 定位后关闭」。

不接受的做法是：按原文照抄「本轮关闭」而不说明 probe 从未执行。

**2026-08-20 owner 决定补记：** owner 批准 D2，选择 (b)：writer-lag 保持 OPEN，直到有界可复现
storage probe 定位根因；若生产前仍无法定位，只能通过一次新的、明确列出残余风险的 owner amendment
决定是否接受，不能沿用本次决定或仅凭 `1,234ms` 数值关闭。

## 5. 下一步（对齐 `GATE_B2_STATUS_20260810_ZH.md` §6）

原计划 §6.7 → §6.8 → §6.9 的顺序不变。按当前状态展开为：

1. **取回并登记本次运行的 exact commit 与全部 artifact digest**（§3）。这是所有后续步骤的前置。
2. **§4.2/§4.3 owner 裁决已完成。** D1：v4 成为 Full-RTH 最终 health authority；D2：writer-lag 保持 OPEN。两项均已记入 `GATE_B2_STATUS_20260810_ZH.md`，并列为后续 assumption review；该 review 不阻塞 B2 freeze，但必须在任何 order-capable Paper/Live 或生产部署前完成。
3. **让交付 commit 自己变绿。** 两条活跃分支已于 2026-08-19 整合（详见 `BRANCH_INVENTORY_20260819_ZH.md` §2.1），
   但 `agent/repo-hygiene-safe` 的 tip `17b3375` **从未跑过 CI**；按 `AGENTS.md`
   「A stale-branch run does not prove the current tree」，合入 `main` 前必须在交付 commit 上有一次自己的绿 CI。
4. **更新 `GATE_B2_STATUS_20260810_ZH.md`**：把「SPY Full-RTH 全日 health」一行按裁决结果改写，
   把本轮 artifact 加入 §5 证据索引，并把 §6.4「下一次完整 RTH 必须使用新 artifact root、唯一 client id 和独立 Task Scheduler host」标注为已执行。
5. **然后才是 §6.8**：逐项完成官方 IB 文档复核，把 B2 的 source、tests、docs、evidence 绑定成一次**覆盖当前树**的新 freeze。
   注意 `STATE.json` 当前仍为 `gate_b1_covers_worktree=false`，B2 收口必须有这次新 freeze，不得沿用 B1 的 `117188cea539`。
6. **最后是 §6.9**：只读证据封存后，由 owner **单独决定**是否授权「paper account、1 股 SPY、机械订单生命周期」子阶段。
   `order_authorization` 当前为 `NONE`，本次 Full-RTH 结果**不构成**该授权。

## 6. 边界声明

- 本文不改变 `STATE.json`，不改变 `gate_b2=READ_ONLY_IN_PROGRESS`，不改变 `order_authorization=NONE`。
- 本文不删除、不覆盖任何既有失败证据；2026-08-12 与 2026-08-13 两轮失败记录保持原样。
- 本文对 2026-08-18 运行的复核限于报告文本、仓库代码与 GitHub CI 可见事实；raw artifact 未经复核方直接读取。
