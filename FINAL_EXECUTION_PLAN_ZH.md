# IB Execution Platform 最终设计与执行方案

版本：v0.1.3.dev0 Phase 0 reviewed  
日期：2026-08-06

## 1. 冻结判断

规格可以冻结，实施不能宣布完成。

必须始终分开两张通行证：

```text
Operational Gate：平台是否能安全、可恢复地运行？
Economic Gate：策略在真实成本和统计不确定性下是否值得交易？
```

任何一张通过都不推导另一张通过。

当前第二个独立使用者：

```text
SECOND_CONSUMER = NONE_CONFIRMED
```

QQQ 不视为独立使用者，因为它与 SPY 属于同一日内动量命题。若 SPY Gate A 为 `NO_GO` 或 `INSUFFICIENT_EVIDENCE`，且仍没有点名独立第二策略，则停止 IB adapter 建设，只保留 recorder、journal、FakeBroker 与通用风险核心。

## 2. V1 范围

```text
一个账户
一个策略
一个标的
一个正常 broker writer
Python 3.12.x production pin
IB Gateway + ib_async
SQLite WAL
```

V1 不做：多策略归因、共享账户、MOC、auction、in-place modify、自动 watchdog takeover、复杂 algo order、微服务化。

## 3. 状态与耦合规则

```text
link_state      : CONNECTED | DEGRADED | DISCONNECTED
sync_state      : UNVERIFIED | SYNCING | SYNCED
operating_mode  : NORMAL | STOP_NEW | FLATTEN_ONLY | HALTED
order_state[s,y]: IDLE | INTENT_COMMITTED | PENDING_ACK | WORKING
                | PENDING_CANCEL | SUBMISSION_UNCERTAIN
                | TERMINAL_UNRECONCILED
```

强制规则：

```text
SUBMISSION_UNCERTAIN or TERMINAL_UNRECONCILED
    => sync_state := UNVERIFIED

任何 broker write
    => link == CONNECTED and sync == SYNCED

增加风险
    => operating_mode == NORMAL

降低风险
    => operating_mode in {NORMAL, STOP_NEW, FLATTEN_ONLY}

HALTED
    => 自动引擎不发任何 broker write；人工 emergency tool 走独立 runbook
```

只有完整 reconciliation 可以提升 `sync_state`；任何单一 quote/order callback 均无权恢复账户级信任。

## 4. 单 writer 与运维边界

进程：

```text
quote_recorder
    只读；独立 Gateway 优先；自身限流与有界退避

execution_engine
    唯一正常 writer

watchdog
    告警；身份验证后 SIGTERM -> SIGKILL
    不重启、不下单、不修改 operating_mode

emergency_flatten
    人工触发；独立 clientId；每月在 paper 演练
```

watchdog 不自动平仓的安全前提是 invariant 19：完全无法日终平仓并隔夜跳空，损失仍在承受范围。任何 size 变更必须与 watchdog ADR 同时重审。

## 5. 下单协议

### 5.1 Target interface

策略只提交：

```text
strategy_id
symbol
target_quantity
decision_id
valid_until
metadata
```

平台自行管理 position、working quantity、cancel、reprice、partial fill、reconnect 和 EOD。

### 5.2 Durable-before-send

```text
1. atomically consume decision_id + TARGET_RECEIVED
2. ORDER_INTENT_COMMITTED
3. SEND_ATTEMPT_STARTED
4. PENDING_ACK
5. broker.place_order
6. SEND_CALL_RETURNED / SEND_CALL_FAILED
7. callbacks append broker facts
```

若无法证明 send 是否到达：`SUBMISSION_UNCERTAIN + UNVERIFIED`，reconcile，绝不盲目 retry。

### 5.3 Cancel/reprice

必须区分：

```text
target_changed  -> cancel -> terminal -> reconcile -> attempt 0 on latest target
reprice_timeout -> cancel -> terminal -> reconcile -> attempt + 1 on same target
flatten         -> durable target 0 -> cancel existing -> converge to zero
```

V1 使用 cancel-then-new，不原地改单。最多 N 次；开仓 exhaustion 后 abandon，风险/EOD flatten 使用单独 policy。

Cancel reject 是非终态 broker fact，不能写成 `ORDER_REJECTED`。

## 6. Reconciliation

顺序：

```text
journal replay
-> broker positions/open orders/executions
-> adopt exact durable-identity facts
-> recompute journal expected
-> compare actual vs expected
-> restore per-leg working state
-> evaluate retained, unexpired latest target
-> SYNCED or HALTED
```

身份链：

```text
decision_id -> intent_id -> orderRef -> orderId -> permId -> execId
```

只接受 exact durable `orderRef`；已知时同时校验 `permId`。前缀不是 ownership。未知 order/execution/fee 立即 HALT，0 股容忍。

真实 IB snapshot 在 Gate B2 采用“双快照稳定屏障”候选方案，是否可行由实测决定，不把 positions/openOrders/executions 预设为原子快照。

## 7. EOD lifecycle

```text
EOD_FLATTEN_STARTED
-> durable target=0
-> cancel/terminal/reconcile
-> closing order
-> EOD_FLATTEN_COMPLETED
```

硬 deadline 时仍有 position 或 working exposure：

```text
EOD_FLATTEN_FAILED(
  residual_quantity,
  working_signed,
  potential_quantity,
  order_state,
  reason
)
```

不得降低 `HALTED`。次日若 journal expected 与 broker actual 一致，启动为 `SYNCED + FLATTEN_ONLY`；不一致则 HALT。

## 8. Miss/availability 账本

临时不可执行：

```text
TARGET_DEFERRED(DISCONNECTED/NOT_SYNCED/ORDER_STATE_BLOCKED/...)
```

只有终局才写：

```text
DECISION_MISSED(EXPIRED/RISK_BLOCKED/BROKER_REJECTED/
                REPRICE_EXHAUSTED/MODE_BLOCKED/...)
```

Phase 5 将 target 的 defer history 与最终 miss 联结。断线后在有效期内成功执行，不应被统计为漏交易。

## 9. 风控

硬要求：

- schema + unknown-key rejection；
- compiled-in sanity bounds；
- startup must-reject self-test；
- config 只可重启生效；
- 每个 intent 保存 config hash；
- orders/minute、orders/day、daily shares、daily notional；
- position 按隔夜承受能力而非日内 buying power；
- quote freshness、spread、collar；
- callback/journal exception fail-closed。

配置验证、runtime counter 与 journal auditor 三者使用同一冻结 limits；daily auditor 不允许使用默认值代替当日生效配置。

## 10. 三条并行轨道

### Track A — Economic Gate，优先级最高

立即完成：

1. SEC Section 31 PIT；
2. TAF/commission/borrow/financing PIT；
3. 可交易 EOD 成交规则；
4. tail stress 成本；
5. circular block bootstrap/HAC；
6. 输出：

```text
GO_TO_DATA_VALIDATION
NO_GO
INSUFFICIENT_EVIDENCE
```

`INSUFFICIENT_EVIDENCE` 对资金部署等同 no-go。

### Track R — Full-RTH recorder，日历约束

记录：BidAsk、AllLast、5-second trades、连接事件、market data type、IB server time、local wall/monotonic timestamps。

每日自动报告：LIVE 权限、三路 stream、RTH coverage、最大 gap、clock skew、重订阅结果、segment/hash。

Recorder 不得伤害交易通路：独立 Gateway 优先；否则强制 token bucket、bounded exponential backoff 和资源隔离。

### Track B — Execution core

只补安全证据，不增加业务功能。

## 11. Gates

### Gate B0：规格冻结

产物：SPEC、ADR、event schema、21 invariants、failure matrix、risk schema、runbook。

### Gate B1：IB-free core

退出条件：

```text
21 invariants 全部具备 P/R/A
进程级 kill-after-WAL/send/cancel 测试
restart/reconnect generated sequences
所有生成 journal auditor PASS
0 duplicate submission
0 external fact silently adopted
0 untrusted broker write
```

Phase 0 timebox 四周。超时先砍 reprice ladder，再砍自动 EOD ladder；不砍 WAL、reconciliation、single writer、risk limits 或 fail-closed。

### Gate B2：IB Paper protocol

1–5 股、人工 target、无 MOC。交付 `documented-vs-observed` 矩阵：orderRef、permId、orderId/clientId、callbacks、1100/1101/1102、fee delay、correction、cancel race、Gateway restart、snapshot stability、paper fill。

### Gate B3：故障演练

kill -9 各关键窗口、Gateway restart、1101、journal I/O failure、bridge overload、callback exception、external fact、stale snapshot、risk config corruption、reject loop、EOD residual。

### Gate C：Shadow

```text
L1 live vs replay: 100% deterministic parity
L2 IB live vs research parquet: differences classified and P&L quantified
L3a IB historical: falsification only
L3b captured live raw feed: no retuning, full/pre/post rerun
```

### Gate D：最小 Live 数据实验

仅在 A、B1、B2、B3、C 均通过后；1 股、无杠杆、独占账户、无 MOC。目的为采集 latency/arrival shortfall/reject/miss，不是资金部署。

### Gate E：成本冻结与资金决策

三账本：

```text
Deterministic fees
Observable micro-size execution costs
Modeled size/capacity costs
```

尾部证据不对称更新：坏于 stress 可立即提高成本/停止；好于 stress 只能继续采样，达到预注册 tail N 与区间宽度后才可下调。

## 12. 当前代码状态

```text
106 deterministic tests PASS
5 Hypothesis-gated tests present (2 generated), not run in review environment
deterministic soak 20 x 50 PASS
editable install PASS
Gate B1 NOT PASSED
DO NOT CONNECT TO IB PAPER OR LIVE YET
```

当前包的正确用途：继续 Phase 0 的证据补全。错误用途：因为测试全绿就开始接 Gateway。
