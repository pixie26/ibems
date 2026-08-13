from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


replace_once(
    "src/ib_execution/market_liveness.py",
    '''        elif code == CONNECTIVITY_RESTORED_DATA_KEPT:\n            self._outages.pop(CONNECTIVITY_LOST, None)\n''',
    '''        elif code == CONNECTIVITY_RESTORED_DATA_KEPT:\n            self._outages.pop(CONNECTIVITY_LOST, None)\n            # 1102 explicitly says market-data requests were maintained.  A\n            # pending timeout recorded while 1100 was suppressing recovery must\n            # not leak through after restoration and manufacture a resubscribe.\n            self._pending_recover = None\n''',
)

replace_once(
    "tests/test_recovery_p0_regressions.py",
    '''    state_1102 = kept.assess(0.1)\n    assert state_1102.action is LivenessAction.CONTINUE\n    assert state_1102.recovery_hint is None\n\n\nclass _FakeIB:\n''',
    '''    state_1102 = kept.assess(0.1)\n    assert state_1102.action is LivenessAction.CONTINUE\n    assert state_1102.recovery_hint is None\n\n\ndef test_1102_clears_a_timeout_pending_behind_1100():\n    liveness = MarketLiveness()\n    liveness.subscription_started(0.0)\n    # Keep the independent BAR heartbeat inside its 12s threshold so this test\n    # isolates the pending recovery created behind 1100.\n    liveness.note_event("BAR_5S", 50.0)\n    liveness.note_status(1100, "Connectivity lost")\n    liveness.note_transport_idle(60.0)\n\n    # The known 1100 outage suppresses the timeout recovery while disconnected.\n    assert liveness.assess(60.1).action is LivenessAction.WAIT\n\n    # 1102 says subscriptions were maintained.  The timeout pending from the\n    # outage must not leak through and trigger a synthetic resubscribe now.\n    liveness.note_status(1102, "Connectivity restored - data maintained")\n    restored = liveness.assess(60.2)\n    assert restored.action is LivenessAction.CONTINUE\n    assert restored.recovery_hint is None\n\n\nclass _FakeIB:\n''',
)

print("1102 follow-up patch applied")
