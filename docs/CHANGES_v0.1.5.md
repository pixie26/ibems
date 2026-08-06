# Changes in v0.1.5.dev0

## Safety fixes

1. Added atomic exact-cause HALT acknowledgement with stale-token rejection.
2. New HALT causes are journaled even when the controller is already HALTED.
3. Restart restores HALT in memory without generating nested synthetic HALT causes.
4. Acknowledgement no longer resumes a live controller.
5. Added `PROCESS_STATE_RESTORED` for auditable restart state.
6. Added mandatory `BrokerSnapshot.is_stable`; unstable snapshots cannot restore `SYNCED`.
7. FakeBroker marks snapshots unstable while position/order callbacks remain pending.
8. Strengthened generated/random sequences and exact position-limit assertions.
9. Added v0.1.4 regression tests for stale HALT acknowledgement and delayed-fill snapshot races.

## Documentation and artifact corrections

- Corrected stale test counts and made the 1,500-example property campaign an explicit command.
- Added ADR-008 and ADR-009.
- Removed cache/bytecode artifacts and duplicate checksum manifests from the release ZIP.
