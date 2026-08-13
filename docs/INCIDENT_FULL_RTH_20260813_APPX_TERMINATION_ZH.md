# Full-RTH Recorder 被 Codex AppX 容器连带终止事故报告（2026-08-13）

- 状态：**OPERATIONAL FAIL / Full-RTH 未通过 / owner 已确认手动关闭 Codex / 独立托管修复已实现并完成本机探针 / 待下一完整 RTH 实测**
- 事故窗口：`2026-08-13 21:20:45–23:35:47 HKT`
- 运行标识：`20260813_full_rth_retest`，Recorder run id `21b4f20bcb`，API client id `966`
- 安全边界：paper Gateway port `4002`、`readonly=True`、SPY `RTH+SMART`、`research_full`；没有 broker write 或 fault injection，`order_authorization=NONE` 不变。

## 1. 结论摘要

本轮不是行情订阅、recovery scheduler、IB Gateway 或 Python 异常导致的正常退出。Recorder 在最后一刻仍持续收到 `LIVE` BidAsk / AllLast / BAR_5S，heartbeat 为 `CAPTURING`、`liveness=continue`、`bar_age=0.25s`。没有 `RECORDER_ERROR`、`10197`、disconnect、writer failure 或 traceback。

直接终止原因已经由 Windows AppModel 事件闭环：承载启动会话的 Codex Desktop AppX 容器 `{098665E2-95E5-11F1-B637-F44EFC7E8F8C}` 在 `23:35:47 HKT` 被销毁（`Microsoft-Windows-AppModel-Runtime/Admin`, event 217）。Recorder 是从该容器内的 Codex 工具进程启动的后代，因此在容器销毁时被连带强制终止。最后状态文件在 `23:35:41.313`，最后完整行情行在 `23:35:41.685`，与容器销毁相隔约 6 秒。

Owner 在事故后明确说明：**2026-08-13 当晚是 owner 本人手动关闭 Codex。** 该 operator testimony 与 event 217 的 container teardown 时间吻合，因此本轮触发条件可以归为 operator-initiated Codex close；Windows 日志本身只证明 container 被销毁，不独立记录谁点击了关闭。

Recorder/IB/recovery 没有再次发生产品故障；剩余问题是一个**托管边界缺陷**：把需要跨整个 RTH 存活的 Recorder 作为桌面 Codex AppX 生命周期内的后台子进程启动。即使明天不主动关闭 Codex，应用重启、升级或其他 lifecycle 事件仍可能终止后代进程，所以 Full-RTH 不应把“operator 记得保持 Codex 打开”作为唯一控制。

修复是通过 Windows Task Scheduler 以当前用户、`InteractiveToken`、`LeastPrivilege` 独立托管 Recorder。新 launcher 严格校验 `STATE.json` 的只读边界，拒绝覆盖已有任务或 artifact root，禁止自动重启，限制单实例和最长运行时间，并将 stdout/stderr 与 launch provenance 写入独立 evidence root。一个 30 秒无 IB 探针已证明计划任务子进程 PID `20836` 的父进程是 Task Scheduler service `svchost.exe` PID `2472`，而不是 Codex PID `24336`；临时任务随后已删除。

## 2. 影响与判定

| 项目 | 判定 |
|---|---|
| Full-RTH 全日 health | **FAIL / 未完成**；约 2 小时 5 分 RTH 前缀不能升级为 PASS |
| 市场数据 | 开盘至 `23:35:41 HKT` 的前缀可读；其后至收盘缺失 |
| 写入完整性 | 26 个已关闭 gzip segment 完整；第 27 个为 abrupt-kill partial，无 gzip footer |
| 尾段可恢复性 | partial 中可解压 `63,881` 个完整 JSONL 行，尾部半行 `0` bytes；仍必须标为 truncated |
| 订单、持仓、资本 | 无影响；运行始终 `READ_ONLY`，最后状态 `net_position=0` |
| Gate B2 | 仍为 `READ_ONLY_IN_PROGRESS` |
| Paper/Live order 授权 | 仍为 `NONE` |

## 3. 时间线

| HKT | 直接观测 |
|---|---|
| 2026-08-12 08:32:49 | Windows 创建旧 Codex Desktop AppX 容器，root process PID `4300`（events 210/211） |
| 2026-08-13 21:20:45.232 | Recorder PID `2644` 从 Codex 工具会话启动；进入 `WAITING_FOR_SESSION` |
| 21:30 后 | 三路 `LIVE` capture 正常开始，5 分钟滚动 segment 持续关闭 |
| 23:34:49.050 | partial 内最后一条 `SERVER_TIME`，connection epoch 始终为 1 |
| 23:35:41.313 | 最后一次 heartbeat publication：`CAPTURING / continue / bar_age=0.25s` |
| 23:35:41.685 | 最后一条完整行情行：`LIVE BID_ASK`，event id / receive sequence `1,570,743` |
| 23:35:47 | Windows event 217：销毁旧 Codex AppX 容器；Recorder PID 不再存在 |
| 23:36:17 | Windows 为 Codex 创建新 AppX 容器；这只能证明应用重新启动，不能证明旧容器为何销毁 |
| 23:53:20 | 独立 Task Scheduler 探针启动；task child parent 为 scheduler `svchost.exe`，随后 probe task 删除 |

事故后 owner testimony 补充：23:35 左右由 owner 本人手动关闭 Codex。该陈述闭合了触发者归因，但仍按 testimony 记录，不把 Windows event 217 描述成能识别点击者的系统审计证据。

## 4. 直接证据

### 4.1 Recorder artifact

根目录：`artifacts/ib_preflight/20260813_full_rth_retest/`

| 文件 | SHA-256 |
|---|---|
| `launch-provenance.json` | `ED2595E8830DBE05B42EADCE3EDBC0C02A2E77B041E1811BD167DB608DC9B8D5` |
| `recorder-status.json` | `5D8EAF0DCF63C5A2C60F952D695FD690697D17BB3432858E44E49C6ECC116476` |
| `.partial-153100-21b4f20bcb-00026.jsonl.gz` | `14DDF5E2506BFC32E1C77ED33339CE38577D8A0167AF7E6AD119860C0D4CC4CE` |

本轮共有 27 个 gzip 文件：26 个 closed segment、1 个 partial；gzip 合计 `24,251,223` bytes，整个 evidence root `24,256,034` bytes。partial 为 `940,074` compressed bytes，可解压 `35,096,063` bytes / `63,881` 完整行；`gzip eof=false`，因此不能把“没有半行”误写成完整 segment。partial 内计数为 BidAsk `56,522`、AllLast `7,298`、BAR_5S `57`、SYSTEM `4`，全部 `market_data_type=LIVE`。

### 4.2 Windows 事件

权威日志：`Microsoft-Windows-AppModel-Runtime/Admin`。

- event 210 at `2026-08-12 08:32:49 HKT`：创建 Codex desktop AppX container `{098665E2-95E5-11F1-B637-F44EFC7E8F8C}`；
- event 211 at same time：root PID `4300` 加入该 container；
- event 217 at `2026-08-13 23:35:47 HKT`：销毁同一 container；
- event 210/211 at `23:36:17 HKT`：创建新的 Codex container；
- Application/System/Defender/WER 在事故窗口没有 Python crash、Application Error、系统重启、睡眠或 Defender 终止证据。

Security log 没有启用可用的 process-termination event 4689；因此无法从该 channel 得到 Recorder 的 exit code 或直接调用者。这不削弱 container teardown 与进程终止的时间/祖先关系，但限制了对“谁触发 teardown”的归因。

## 5. 根因分层

| 层级 | 当前结论 | 证据状态 |
|---|---|---|
| 最终终止机制 | Codex Desktop AppX container 被销毁，后代 Recorder 被强制终止 | **verified** |
| 设计根因 | 长时 Recorder 错误依赖交互式 Codex/AppX 生命周期 | **verified** |
| 终止前 Recorder 状态 | 三路 LIVE、heartbeat 正常、无 app-level fault | **verified** |
| IB Gateway / subscription fault | 与最后时刻的 LIVE 数据、epoch 1、无 error 不符 | **排除为本轮直接原因** |
| Python exception / graceful exit | 与零 stderr、无 FAILED/STOPPED、partial 无 footer 不符 | **排除** |
| Codex container 销毁的原始触发者 | owner 明确确认本人手动关闭 Codex；event 217 独立确认 teardown | **owner testimony + corroborating system event** |

Recorder 从启动到最后数据约 `8,096.45s`，表面接近 2 小时 15 分。更强的 AppModel 证据显示 container 实际从前一日已存在，所以不能把本轮写成“Codex 固定 8,100 秒 timeout”。

## 6. 修复

新增 `scripts/start_full_rth_recorder_task.py`：

1. 只在 Windows 上执行真实注册；`--validate-only` 可无副作用检查完整计划。
2. 启动前验证 `gate_b2=READ_ONLY_IN_PROGRESS`、`order_authorization=NONE`、`trading_adapter=NOT_IMPLEMENTED`。
3. artifact root 必须是 `artifacts/ib_preflight/` 下的新子目录；status 必须位于同一 root，避免覆盖事故证据。
4. task name 必须唯一；已有同名 task 时 fail closed。
5. Task Scheduler principal 为当前用户 `InteractiveToken + LeastPrivilege`；不提升权限。
6. 允许 AC/DC 启动且切换电池不自动停止；最长运行默认 8 小时；`IgnoreNew` 防重复实例。
7. 不配置 `RestartOnFailure`；Recorder 或 task 失败后由人复核，不自动重启。
8. stdout、stderr 和 `task-launch.json` 进入该次 evidence root；launch record 绑定 Git commit/branch、
   dirty worktree 列表、source/config/lock hashes 与实际 Task XML SHA-256。

PowerShell `.ps1` 原型因本机 execution policy 被拒绝；整改没有使用 `ExecutionPolicy Bypass`，而是改为 Python 生成最小 Task Scheduler XML 后调用系统 `schtasks.exe`。

建议下一完整 RTH 使用**全新日期/root/client id**，不要续写本事故目录。例如：

```powershell
.\.venv312\python.exe scripts\start_full_rth_recorder_task.py `
  --task-name ibems-full-rth-YYYYMMDD `
  --artifact-root artifacts\ib_preflight\YYYYMMDD_full_rth_task `
  --client-id NEW_UNIQUE_CLIENT_ID
```

运行后先核对 `schtasks /Query /TN <task-name> /V /FO LIST`、新 status PID、Gateway listener、heartbeat 与 gzip 增长。正常 finalize 并完成证据封存后，再执行 `schtasks /Delete /TN <task-name> /F`；运行中不得为了清理任务而删除或终止。

## 7. 验证状态与残余风险

- `verified`：直接终止机制、artifact 尾部、AppX 时间线、独立 Task Scheduler 父进程、临时 task 已删除。
- `owner testimony`：owner 本人手动关闭 Codex，触发 container teardown；系统事件印证 teardown，但不识别点击者。
- `locally verified`：launcher validation、路径/STATE fail-closed、最小 task XML 和无 IB detached probe。
- `not verified`：修复后的下一次完整 RTH；Task Scheduler 宿主跨 Codex 容器销毁的整日 soak；正常收盘 finalize。
- Full-RTH 仍未通过；本事故不能由同日 continuation 补成 PASS。
- 当前 worktree 改变后不在既有 attestation 覆盖内；`STATE.json` provenance 已按当前树重新生成并通过检查，但仍须在后续 freeze 中绑定 exact source/config/lock/evidence。
