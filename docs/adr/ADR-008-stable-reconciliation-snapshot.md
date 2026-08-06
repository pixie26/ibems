# ADR-008 — Stable reconciliation snapshot is required to restore SYNCED

## Decision

`BrokerSnapshot` carries `is_stable`. The controller refuses to restore `SYNCED`
unless it is true. The adapter may set it only after an explicit broker-side
completion/watermark barrier across positions, open orders and executions.

## Why

A cancelled order can disappear from one snapshot while a delayed execution is
still in flight. Sending a replacement from that view can create duplicate exposure
and breach the position limit. A socket connection and a set of completed requests
do not by themselves prove a coherent account state.

## Consequences

- Unstable snapshots are diagnostic only.
- Gate B2 must measure and document the actual IB barrier protocol.
- Reconnect, cancel and restart paths remain `UNVERIFIED` until the barrier completes.
