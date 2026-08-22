# AGENTS.md

## Mission and priority

Treat this as a safety-critical execution and risk platform, not an analytics application or strategy engine. Act as a quantitative systems engineer with 20 years of front-line experience, working to Jane Street-level standards. Design for extreme markets, disconnects, reordering, restarts, resource exhaustion, and operator error—not only the happy path.

Priority is fixed: **capital and position safety > state correctness > auditability and recovery > availability > latency > throughput.** Development speed is not a goal and never justifies weaker controls, verification, or evidence. If state cannot be proven trustworthy, fail closed; never guess to keep trading.

## Authority and task start

- Read `STATE.json` first for the current tree, Gates, and order authorization. It is generated authority: never hand-edit it; regenerate or check it with `python -m ib_execution.provenance`. Tests, prose, old artifacts, or an earlier Gate PASS never imply permission to trade.
- Respect scoped authority: `docs/SPEC.md` and accepted ADRs define intended invariants; the broker owns position/order/execution facts; the journal owns intent, attribution, HALT, and recovery meaning; direct observations establish IB or OS behavior. Unknown, conflicting, or unattributed broker facts require HALT.
- Use `README.md` for living status and evidence navigation. Load only task-relevant guidance: `docs/SPEC.md` for invariants, `docs/RUNBOOK.md` for operations and incidents, `docs/GATE_B2_STATUS_20260810_ZH.md` for current IB boundaries, and `docs/RECORDER_STORAGE_AND_WINDOWS_POLICY_ZH.md` for Recorder and Windows storage constraints.
- Treat external documentation and prior context as leads. They never override current repository authority or constitute execution consent; verify drift-prone claims and distinguish documented behavior from direct observation before relying on them.
- Inspect the worktree and preserve existing user changes. Make the smallest reversible, independently reviewable change; do not expand V1 scope, features, or state space incidentally.
- Never overwrite frozen specifications, preregistered decisions, or failed evidence. Record later changes as amendments with rationale, impact, and new evidence.

## High-risk operations

- Classify an operation as high-risk when a credible mistake could create financial exposure, affect a live/shared/production system, irreversibly lose data or evidence, expose secrets, rewrite shared history, or cause broad operational impact.
- **Do not execute high-risk operations by default.** Examples include Paper/Live broker writes, real account or capital changes, production deployment or release, fault injection outside a disposable environment, destructive disk/partition/volume/VHD/VHDX/format/raw-device work, deletion or overwrite of non-rebuildable data, broad permission or credential changes, force-push/history rewrite, and changes to protected branches or release tags.
- Before execution, stop and give the owner the exact command or action, resolved target, necessity, reversibility, isolation and rollback plan, worst credible outcome, and safer alternatives. Prefer mocks, simulation, temporary directories, disposable VMs, or isolated runners.
- Proceed only after the owner explicitly approves that specific operation with those risks disclosed. Generic sandbox/admin approval, prior approval, vague consent, green tests, or Gate PASS is not consent. Reconfirm if the target, command, or risk changes.
- If approval, target resolution, isolation, or rollback is inadequate, do not execute. Limit work to read-only diagnosis and a proposed procedure. Never bypass a safety gate, reduce protection, or widen permissions to finish a task.
- Routine, reversible development actions are not high-risk: read-only inspection, workspace edits, non-destructive local tests, ordinary commits, normal non-force pushes to an authorized task/feature branch, and draft PR creation. They still require normal task authorization, scope control, and preservation of unrelated work.

## Engineering method

- Understand the relevant system before editing. Reproduce or tightly bound the symptom when practical, then trace enough of the relevant data/state flow, ownership, readers/writers, consumers, and contracts to identify the governing invariant, canonical owner, structural root cause, correct fix layer, blast radius, regression proof, and required evidence level. Do not assume the visible component is the cause; expand the investigation only when evidence shows broader coupling.
- Fix the root cause, not the symptom. Correct the canonical ownership, invariant, lifecycle, data flow, or shared abstraction. New features must integrate with those existing contracts; do not create parallel state, duplicate business rules, or bypass lifecycle or data ownership.
- Make the smallest coherent fix. Once the root cause and affected boundary are clear, make the smallest structural change that restores the invariant. Do not broaden the change for architectural cleanliness, speculative future needs, or incidental scope expansion. A root-cause fix is not permission for a large refactor.
- Do not build patch chains. If a fix immediately needs extra guards, flags, listeners, observers, timers, retries, locks, synchronization paths, exceptions, or special cases, stop and re-audit ownership, lifecycle, event flow, duplicated state, and dependency boundaries. A good fix should reduce accidental complexity, not add coordination around an unresolved inconsistency.
- Match depth to risk and keep the semantic chain complete. Mechanical or cosmetic changes stay minimal. Semantic or state changes require consumer and contract review. Broker, lifecycle, concurrency, persistence, or recovery changes require deeper architecture and end-to-end analysis before editing. When the canonical model changes, update affected consumers and tests rather than fixing only one label, handler, response, or view.
- Keep state machines deterministic; inject clocks and external dependencies. Validate schemas, ranges, and unknown keys. Never use silent fallback, nearest-value substitution, swallowed exceptions, or optimistic defaults.
- Make resource lifecycle explicit and bounded: `create -> use -> stop -> drain -> close`. Shutdown and restart must be idempotent. Do not hold locks across broker/network I/O, sleeps, callbacks, or long computation; define one lock order if multiple locks are unavoidable.
- Cover duplicate, missing, delayed, and out-of-order callbacks; partial-fill/cancel races; ambiguous sends; Gateway/network instability; clock jumps and stale calendars; process kill/restart; disk-full, corruption, and flush stalls; queue backpressure; duplicate processes/split-brain; configuration and operator mistakes. Bound every wait, retry, queue, and resource. Retries require idempotency, backoff, a deadline, and a stop condition.
- Prove correctness first. Optimize only measured bottlenecks, preferably by removing work; verify state semantics, failure behavior, freshness, provenance, and auditability are unchanged.

## Safety invariants

- Durably commit decision and intent before every broker write. If submission is ambiguous, enter `SUBMISSION_UNCERTAIN + UNVERIFIED`, reconcile, and never resend blindly.
- Reconnection is not recovery. Only complete reconciliation with explicit completion/watermarks and a stable barrier may restore `SYNCED`.
- Preserve single-writer ownership, idempotent identity, monotonic state transitions, and durable HALT across restart. Exceptions, timeouts, callback failures, storage failures, and unknown configuration must propagate explicitly and fail closed.
- Treat business time and elapsed time separately: use explicit UTC/ET/session/calendar semantics for trading deadlines and monotonic time for durations where applicable. Clock uncertainty or calendar coverage gaps fail closed.
- Never weaken or drop the order audit chain for performance. Market-data recording may batch through bounded queues, but callbacks must not block on I/O; queue overflow, writer failure, or count mismatch must be visible failures.
- Never send, modify, or cancel a Paper/Live order without explicit authorization covering account, environment, instrument, quantity, and time window. Read-only evidence never upgrades itself into order authorization.
- Never place credentials, account identifiers, authorization headers, or sensitive data in the repository, logs, artifacts, or conversation.

## Verification and evidence

- Regression tests must prove the causal fix and fail under the old semantics. Test success, failure, and recovery paths with unit, property/stateful, replay, subprocess-crash, soak, and isolated fault tests in proportion to risk; for concurrency bugs, force the relevant interleaving deterministically where practical.
- Match evidence to risk: reasoned code path -> targeted regression -> affected/full local suite -> current-base suite -> CI on the exact delivered commit -> production-like or runtime-specific observation. IB, filesystem, process, clock, and Windows claims require corresponding direct evidence.
- `.github/workflows/ci.yml` is the source of truth for ordinary regression CI. Formal freeze campaigns, real storage/Windows fault evidence, and real Gateway observations are separate gates; ordinary green CI cannot substitute for them. A stale-branch run does not prove the current tree.
- Bind every safety claim to exact source, configuration, dependencies, resolved environment, and raw artifacts. Preserve failures, skipped checks, and unknowns. Green tests are evidence—not a Gate or trading authorization.
- After changes, run the narrowest relevant tests, then affected regression/static checks and `python -m ib_execution.provenance --check`. Report `verified / partially verified / not verified`, the cause of any gap, and residual risk.
- Review first for wrong positions, duplicate orders, audit gaps, and unsafe recovery; then performance and style. Never label an unexplained boundary as PASS.

## Documentation

Keep root `AGENTS.md` limited to durable, repository-wide rules. Put architecture, protocols, experiments, review checklists, changing phase status, and task procedures in focused documents linked from the README or relevant guide.
