# ADR-007 — Synchronous core, asynchronous bridge

**Decision:** keep the Phase-0 controller synchronous and deterministic, but
never execute it on the `ib_async` event-loop thread. `AsyncControllerBridge`
serializes all broker callbacks and strategy commands through one dedicated
worker thread.

A writer thread inside `Journal` moves the SQLite syscall, but a synchronous
`commit()` still waits for durability. Calling that from the IB loop would
therefore block callback processing. The bridge is required before Gate B2.

The queue is FIFO and the executor has one worker, preserving the single-writer
state-machine property. Queue overflow schedules `HALTED` on that same executor;
Gate B3 must verify the overflow, journal-failure, and alarm paths.
