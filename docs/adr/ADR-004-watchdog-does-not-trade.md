# ADR-004 — The watchdog alerts and kills; it does not trade

**Status:** Accepted · **Date:** 2026-08-06 · **Coupled to:** invariant 19

## Context

The appealing design is: heartbeat lost + position open → watchdog flattens.

It fails on a case that is more likely than the one it protects against. The
engine may be **alive** while its status file is stale — blocked fsync, wedged
filesystem, network partition between hosts. The watchdog concludes death,
starts flattening, and now two processes act on one account.

A duplicate or opposing position from split-brain is worse than an unattended
one. Doing takeover safely needs a lease, a fencing token, single-writer
ownership, and broker re-verification. That is a distributed-systems project.

Writing a shared `operating_mode = STOP_NEW` is also unreliable: a process
wedged on fsync will never read it. **Anything reachable by a file write is
usually not the process that needs stopping.**

## Decision

Watchdog **may**: detect heartbeat loss · check the Gateway · read the last
broker snapshot · alert · `SIGTERM` then `SIGKILL`.

Watchdog **may not**: place or cancel orders · flatten · adopt working orders ·
restart the engine · write `operating_mode`.

`SIGKILL` is the only fencing action that does not require the target's
cooperation. Restart is a human decision, because an automatic restart would
reconcile and resume trading with the root cause undiagnosed.

Recovery path is `emergency_flatten.py`: separate process, dedicated clientId,
explicit typed confirmation, **drilled monthly on paper**. A recovery tool that
has never been run is a file, not a tool.

## Consequences

**This is only safe because of invariant 19** — position limits sized so an
unflattened position survives overnight. The two are coupled: any change to
position size must revisit this ADR in the same commit.

Single-writer is a stronger safety property than automatic flattening, so we
keep the former and give up the latter.
