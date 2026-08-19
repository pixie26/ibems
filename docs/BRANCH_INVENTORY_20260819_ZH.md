# 分支清点与整合方案（2026-08-19）

基线：`origin/main = cbd1289`（2026-08-15，"fix first rth"）。共 17 条非 main 远端分支。

## 1. 结论

- **15 条可清理**，其中 14 条零风险，1 条需要先保全一个文件。
- **2 条是活跃工作线**，且**已经分叉**，必须先整合成一条再谈合并。
- 仓库当前**没有任何分支记录 2026-08-18 的 Full-RTH 结果**；`main` 也**不含**产生该结果的 v4 代码。

## 2. 活跃分支（必须处理）

| 分支 | tip | 领先 main | CI | PR |
|---|---|---|---|---|
| `agent/full-rth-v4-repair` | `34f3ac4` | 28 | **windows-verify FAILURE**（2026-08-19） | #14 open / **draft** |
| `agent/repo-hygiene-safe` | `17b3375` | 29 | **从未运行** | 无 |

两者共享前 27 个 commit，之后分叉：

- `full-rth-v4-repair` 独有 `34f3ac4`：新增 `CLAUDE.md`（一行 `@AGENTS.md`）
- `repo-hygiene-safe` 独有 `a253a4a`（`.gitignore` 加固）、`17b3375`（根 `FINAL_EXECUTION_PLAN_ZH.md` 与当前 Gate 状态分离）

三个 commit 互不冲突，无技术障碍，纯粹是分叉未收敛。

共同带来的实质内容（`main` 全部没有，`+4,097 / -255`，18 个文件）：
`src/ib_execution/recorder_health_v4.py`、`scripts/reanalyze_full_rth_v4.py`、
`scripts/run_full_rth_recorder_task.py`（同进程 task host + PT24H backstop）、
`scripts/validate_full_rth_finalize_replay.py`、`scripts/verify_windows_full_rth_task_lifecycle.py`，
以及 `market_liveness.py` 的 farm ownership 重写与四组新回归测试。

### 2.1 建议整合方式

把 `34f3ac4` 的 `CLAUDE.md` 并入 `repo-hygiene-safe`，形成唯一交付分支，再把 PR #14 的 head 换过去
（或另开 PR，关闭 #14 并在其中注明继任分支）。

```bash
git checkout -B integrate/full-rth-v4 origin/agent/repo-hygiene-safe
git cherry-pick 34f3ac4          # CLAUDE.md，一行
```

### 2.2 合并前必须先绿

`repo-hygiene-safe` 的 tip `17b3375` 从未跑过 CI。按 `AGENTS.md`
「A stale-branch run does not prove the current tree」，交付 commit 必须有自己的一次绿 CI。

当前 PR #14 唯一的红是 `windows-verify`：

```
FAILED tests/test_journal_fail_closed.py::test_storage_error_fences_before_broker_write[wal-corrupt]
  ib_execution.journal.JournalUnavailable: journal write timed out after 0.1s
  1 failed, 504 passed, 1 skipped in 184.52s
```

判定依据（三条独立证据指向 runner 计时，而非回归）：

1. 失败点在 `_system()` 建 Controller 时的**首次 journal commit**，发生在注入 `storage_error` **之前**；
   被测语义（storage error → fence → 无 broker write）根本没跑到。
2. `d4cf5bf` 于 2026-08-15 全绿；`34f3ac4` 相对它**只多了一行 `CLAUDE.md`**，不触及任何被测代码。
3. 本地 Linux 上该测试连跑 20 次全过；同一树完整套件 `502 passed, 4 skipped`。

根因是测试自身的计时假设：`tests/test_journal_fail_closed.py` 的 `_system()` 把
`write_timeout_seconds` 和 `sqlite_timeout_seconds` 都固定为 `0.10`，而该次写入含一次真实 fsync。
Windows 托管 runner 负载抖动时超过 100ms 属于正常范围（该次日志中同一区段耗时 105 秒）。
这与 PR #12 修掉的「Windows process-lock 测试假设」是同一类问题，**不是产品缺陷**。

建议修法：给 `_system()` 一个平台相关的下限（例如 Windows 上取 `max(0.10, 1.0)`），
或把「建 Controller」与「注入故障后计时」两段的 timeout 分开——被测语义只需要后者短。
不接受的修法：整体放宽超时到掩盖真实 journal 停滞的程度，或跳过该测试。

## 3. 可清理分支

### 3.1 已是 main 祖先（9 条，零风险）

| 分支 | tip | 对应 PR |
|---|---|---|
| `agent/refine-high-risk-definition` | `1c10252` | #10 |
| `b1-attestation-117188cea539` | `c102e97` | #8 |
| `b1-ci-provenance-clean-worktree` | `8dc9ef5` | #7 |
| `b1-derived-pass-evidence` | `6235bd2` | #3 |
| `b1-owner-acceptance-governance` | `26ba3c5` | #5 |
| `b1-signoff-protocol` | `4cf44c2` | #2 |
| `claude/ib-preflight-validation-review-lx0sbl` | `a2634ec` | #1 |
| `claude/ntfs-vhd-disk-full-test-a0ybm8` | `aa3173b` | — |
| `codex/b2-readonly-hardening-review` | `5b08e31` | #9 |

### 3.2 非 main 祖先，但内容已被取代（5 条，逐条已复核）

| 分支 | tip | 复核结论 |
|---|---|---|
| `agent/recovery-p0-clean` | `d8a5d51` | `git cherry` 判定与 main 的 `e91affa` patch-id 等价 |
| `agent/fix-ci-baseline` | `e17fb20` | `.github/workflows/ci.yml`、`test_gateway_detection.py`、`test_process_lock.py` 与 main **逐字节相同**；余下 8 个 commit 是 bootstrap/retrigger 脚手架 |
| `agent/consolidate-full-rth-retest-20260813` | `dd71b73` | `INCIDENT_FULL_RTH_20260812_ZH.md` 与 main 相同；`GATE_B2_STATUS` 的差异是**更旧**的文本（写于 AppX 事故之前），main 更新 |
| `agent/recovery-p0-unified` | `0d614a5` | 唯一有产品价值的 `0d614a5`「Honor 1102 maintained subscriptions」已在 main 的 `market_liveness.py:480` 逐行存在；其余为临时 patcher/CI 脚手架 |
| `b1-attestation-7a8435369303` | `2419670` | 无 branch-only 文件；已被 `117188cea539` 冻结取代 |

### 3.3 删除前需先保全（1 条）

`b1-attestation-27d027ff390b`（`5ea2424`）带有一个**仓库其他任何地方都没有的文件**：

```
docs/GATE_B1_SIGNOFF_27d027ff390b.md
```

这是一份针对已被取代的冻结点的 B1 sign-off packet（撤回稿）。按 `AGENTS.md`
「Never overwrite frozen specifications, preregistered decisions, or failed evidence」，
删除分支前应先打不可变 tag 保全：

```bash
git tag archive/b1-attestation-27d027ff390b origin/b1-attestation-27d027ff390b
git push origin archive/b1-attestation-27d027ff390b
```

保全之后再删除。

## 4. 执行清单（需 owner 批准后执行）

删除远端分支属于共享历史变更，不在「routine, reversible development actions」范围内，因此不默认执行。

```bash
# 1) 先保全（唯一有独有内容的分支）
git tag archive/b1-attestation-27d027ff390b origin/b1-attestation-27d027ff390b
git push origin archive/b1-attestation-27d027ff390b

# 2) 再清理 15 条
git push origin --delete \
  agent/refine-high-risk-definition \
  b1-attestation-117188cea539 \
  b1-ci-provenance-clean-worktree \
  b1-derived-pass-evidence \
  b1-owner-acceptance-governance \
  b1-signoff-protocol \
  claude/ib-preflight-validation-review-lx0sbl \
  claude/ntfs-vhd-disk-full-test-a0ybm8 \
  codex/b2-readonly-hardening-review \
  agent/recovery-p0-clean \
  agent/fix-ci-baseline \
  agent/consolidate-full-rth-retest-20260813 \
  agent/recovery-p0-unified \
  b1-attestation-7a8435369303 \
  b1-attestation-27d027ff390b
```

清理后剩：`main` + 一条整合分支 + 本次复核分支。

## 5. 顺序建议

清理（§4）与整合（§2）互不阻塞，但**整合优先**：`main` 目前不含 v4 代码，而 2026-08-18 的
Full-RTH 结果依赖 v4。在整合落地之前，`main` 无法解释仓库自己产出的验收报告。
