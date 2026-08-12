# 非本机环境能否完成 #6 / #7 故障演练

日期：2026-08-12。对应 [`RECORDER_STORAGE_AND_WINDOWS_POLICY_ZH.md`](RECORDER_STORAGE_AND_WINDOWS_POLICY_ZH.md)
「仍未解除的授权边界」一节列出的 Windows 前置证据。

本文回答一个操作性问题：第 6 项（NTFS VHD disk-full）和第 7 项（flush stall / WAL / witness /
强杀）是否可以在**不使用 owner 本机磁盘**的前提下完成，以及在哪种环境下完成到什么程度。

结论先行：**能搬走的比想象中多，但"搬到 Linux 云 VM"和"搬到隔离 Windows runner"是两件不同的事，
只有后者能减少 Windows 授权边界上的欠账。** 第 6 项已在隔离 Windows runner 上实测通过
（run 31562522619，见下）；第 7 项只有一部分能搬走，flush stall 哪里都做不了。

## 三类环境的能力差异

| 能力 | 本次云 Linux VM | GitHub Actions `windows-2025` | owner 本机 Windows |
|---|---|---|---|
| root / 提权 | 有（含 `CAP_SYS_ADMIN`） | 有（runner 为提权管理员） | 有 |
| loop device / 独立小卷 | 有（`/dev/loop0-7`） | 有（`diskpart` VHD） | 有 |
| 真实 NTFS **on-disk 格式** | 有（`mkntfs` + `ntfs-3g`） | 有 | 有 |
| 真实 NTFS **驱动语义**（`ntfs.sys`、Cache Manager、`MoveFileExW`） | **无** | 有 | 有 |
| device-mapper / `dm-delay` | **无**（该内核未提供 device-mapper） | 无（Windows 无对应机制） | 无 |
| FUSE | 有（`/dev/fuse`） | 无（需 WinFsp/Dokan，未引入） | 无 |
| 是否属于 owner 本机磁盘 | 否 | 否 | 是 |

这张表里唯一重要的一行是第四行。ntfs-3g 只复刻 NTFS 的**磁盘格式**，写入路径是 Linux FUSE，
不是 Windows 内核的 `ntfs.sys`。因此在云 Linux VM 上跑出来的 NTFS 结果**不能**用来注销
Windows 侧的欠账，它证明的是被测对象（execution host 的 fail-closed 行为）而不是宿主平台。

## 本次云 Linux VM 实测

环境：Ubuntu 24.04 / Linux 6.18.5 一次性 microVM，Python 3.12.3，
commit `7ae3a866dff9d38086d17e4a665491f98e598513`，`worktree_clean=true`，
`source_tree_sha256=71e89ae0ba0e…`。

卷的构造（journal 卷与 fence 目录 `st_dev` 不同，drill 自身会拒绝同卷）：

```bash
truncate -s 192M /opt/ibems-drill/ntfs.img
mkntfs --force --fast --label IBEMS_DRILL /opt/ibems-drill/ntfs.img
ntfs-3g $(losetup -f --show /opt/ibems-drill/ntfs.img) /mnt/ibems-ntfs
```

`ntfs-3g 2022.10.3`，挂载类型 `fuseblk`，192MB。

### disk_full — PASS

`run_storage_fault_drill.py --drill disk_full --journal-volume /mnt/ibems-ntfs`

- ballast 写满 193,200,128 bytes，维持零剩余（WAL checkpoint 释放的空间被持续回收）；
- host 子进程退出码 `10` = `EXIT_FATAL_SHUTDOWN`，与期望一致；
- fence `RAISED`，reason 为真实 SQLite 错误
  `journal write failed: database or disk is full`；
- witness 存在（`seq=12`，`SEND_ATTEMPT_STARTED`）。

这是真实 ENOSPC，不是注入异常。

### wal_corruption — PASS

- 破坏后重放到 1377 条事件，WAL recovery 丢弃 55 条**已提交**事件，全部位于 witness
  `seq=735` 之上 ⇒ 没有丢失 send 或 HALT，启动被判定为正确；
- 强制越过 witness（从 `seq=735` 起截断）后：退出码 `15`，fence `RAISED`，理由为
  "journal ends at seq 734 but a broker write was authorised by seq 735"。

即 B1.6 witness 的判别力在这套存储上仍然成立。

### fsync_stall — INCONCLUSIVE（不是 PASS，也不是 FAIL）

首选机制 `dm-delay` 在本环境不可用：该内核没有 device-mapper（`/proc/devices` 无
`device-mapper`，无 `/dev/mapper/control`，镜像内也无 `dmsetup`/`modprobe`）。

drill 退回 FUSE `slow_fsync_fs.py`。补齐 `libfuse2` 与 `fusepy==3.0.1` 后在一个干净的
ext4 loop 卷（`-m 0`，256MB）上重跑，得到的是确定结论而不是"可能不行"：

> SQLite WAL is unusable on this mount (signal 7). A FUSE passthrough cannot back
> the `-shm` file WAL mode mmaps.

即被测进程死于 SIGBUS，根本没有走到 30 秒写超时判定。drill 记为 `inconclusive` 而非任一
结果，这是对的：一个跑不起来的演练对平台没有任何断言。

**因此 flush stall 在本环境无法完成，且不是缺依赖或缺权限，是内核不提供 device-mapper。**

## 隔离 Windows runner（`.github/workflows/windows-ntfs-fault.yml`）

这是唯一能真正减少 Windows 欠账、同时又不碰 owner 本机磁盘的环境。工作流已经存在，
`workflow_dispatch` 手动触发，`windows-2025`，只在 `artifacts/` 下新建 192MB VHD 并在结束时
detach + 删除。

它当前覆盖：

- `run_windows_ntfs_safe_drill.py`：100 代 durable replace 回读、两进程单一 owner、
  holder 强杀后 successor 取锁、publication writer 中途强杀后目标仍是完整 JSON 代
  ⇒ 对应第 7 项里的 **fence/witness publication 强杀** 与 ownership 部分；
- `run_windows_ntfs_vhd_disk_full.ps1`：隔离 VHD 上的真实 NTFS disk-full
  ⇒ 对应第 **6** 项。

### 本次修复：解释器解析

`run_windows_ntfs_vhd_disk_full.ps1` 原先硬编码 `.venv312\python.exe`。那是本机 Windows 约定，
而 workflow 用 `uv sync` 生成的是 `.venv\Scripts\python.exe`。在 runner 上该行必然失败，
**并且是在 VHD 已经创建并挂载之后失败**。

改为：`-PythonExe` 参数优先，其次按
`.venv312\python.exe` → `.venv312\Scripts\python.exe` → `.venv\Scripts\python.exe` →
`.venv\python.exe` 顺序探测（沿用 `run_ib_gateway_outbound_fault.ps1` 已有写法），
且**解析被移到任何磁盘操作之前**——解释器缺失不应该留下一个已挂载的 VHD。

在 Linux 上用 PowerShell 7 验证过三条路径：脚本解析无误；不带 `-PythonExe` 时在
`diskpart` 之前抛错且不产生任何 VHD 或 diskpart 脚本；带 `-PythonExe` 时正常推进到
`diskpart` 步骤（此处因 Linux 无 `diskpart.exe` 而停止，属预期）。

### 实测：run 31562522619 — 两项均 PASS

修复后经 owner 明确批准，于 2026-08-12T04:12Z 在 `windows-2025` 托管 runner 上
`workflow_dispatch` 执行，ref = `claude/ntfs-vhd-disk-full-test-a0ybm8` @ `45c20e5`。
**这是本平台第一次在真实 Windows NTFS 驱动上直接观察到 disk-full 行为。**

`run_windows_ntfs_safe_drill.py`（`filesystem: "NTFS"`，`passed: true`）：

| 检查 | 结果 |
|---|---|
| `ntfs_durable_replace_readback` | PASS |
| `two_process_single_owner` | PASS |
| `force_kill_releases_kernel_lock` | PASS |
| `publication_force_kill_leaves_complete_generation` | PASS |

`run_windows_ntfs_vhd_disk_full.ps1`（192MB fixed VHD，`diskpart` 格式化并挂为 `R:`）：

- ballast 写满 180,879,360 bytes；
- host 子进程退出码 `10`，与期望一致，`timed_out: false`；
- fence `RAISED`，`journal_path` 为 `R:\drill-disk_full\journal.db`，reason 同为真实
  SQLite 错误 `journal write failed: database or disk is full`；
- witness `seq=12`（`SEND_ATTEMPT_STARTED`），
  `digest=996b57cb33b336aa…`；
- host 记为 `runnervm7vqe0`，作业结束后 runner 销毁。

Windows 上观察到的 ballast 字节数低于 Linux（180.9MB vs 193.2MB），与 NTFS 的 MFT 保留
区一致；两侧都在真实 ENOSPC 处停下并 fence，行为判定相同。

证据封存：artifact `windows-ntfs-fault-45c20e5defc15cd265fbfe2db92609dde51b2fec`，
id `9128253449`，zip SHA-256
`a5f2b341ac633b78e61ce4e4524ef5273bfb58af804da2584c7c98389a33eb2f`，留存 90 天。
`finally` 分支完成 detach 与删除；runner 清理阶段终止了一个 `vdsldr`（虚拟磁盘服务
加载器）孤儿进程，属 `diskpart` 常规残留，不影响卷状态。

## 仍然不能在任何非本机环境闭环的项

- **Windows flush / fsync stall。** Windows 没有 dm-delay 等价物；在托管 runner 上要么引入
  WinFsp/Dokan 文件系统 shim（新依赖、且与 Linux 侧同样的 mmap 疑虑），要么装过滤驱动
  （托管 runner 不可行）。目前无解，应继续留在 B3。
- **Windows 上的 WAL damage/rollback + witness crossing。** 机制上没有障碍——
  `run_storage_fault_drill.py --drill wal_corruption` 是纯 Python，同一个 VHD 卷即可承载——
  但当前 `run_windows_ntfs_vhd_disk_full.ps1` 只传 `--drill disk_full`。这是一个待批准的扩展，
  不是能力缺口。
- **execution service 强杀、ownership 继承与 startup refusal、volume failure-domain 判定。**
  需要真实 Windows 服务安装，托管 runner 上可做但尚无脚本。
- **生产等价卷。** VHD 之于真实生产卷，仍是近似；这一条只能由 owner 的部署环境回答。

## 对当前 Gate 的影响

`order_authorization` 仍为 `NONE`，上述结果都不构成 Windows order-capable 授权。绿色演练是证据，
不是 Gate。

第 6 项的性质变了：它此前是"未观测"，现在是"已在隔离 Windows runner 上直接观测并封存"。
但**是否把托管 runner 上的 192MB VHD 认作"生产等价 OS/volume"，是 owner 的风险接受判断，
不是本文能替代的结论**——因此
[`RECORDER_STORAGE_AND_WINDOWS_POLICY_ZH.md`](RECORDER_STORAGE_AND_WINDOWS_POLICY_ZH.md)
的授权边界清单不由本次运行自动划掉。真正未被这次运行触及的是：真实生产卷的几何与驱动栈、
以及订单 Journal 进入真实 broker 路径后应以 Journal fail-closed 为被测对象这一条。

**更新（2026-08-12）：owner 已回答该判断 —— 算。** 因此授权边界清单中的 NTFS disk-full 与
fence/witness publication 中途强杀两项已解除，记录见
[`RECORDER_STORAGE_AND_WINDOWS_POLICY_ZH.md`](RECORDER_STORAGE_AND_WINDOWS_POLICY_ZH.md)
Amendment 1 与 [`GATE_B2_STATUS_20260810_ZH.md`](GATE_B2_STATUS_20260810_ZH.md) §3.1。上一段列出的
两项未触及事项不受该接受影响，仍然成立；`order_authorization` 仍为 `NONE`。

第 7 项维持推到 B3 前：publication 强杀与 ownership 已随本次运行在真实 NTFS 上通过，
WAL/witness 在 Windows 上是一行参数的待批准扩展，而 flush stall 无解。
