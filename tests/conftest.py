from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

try:
    from hypothesis import HealthCheck, settings
except ImportError:  # deterministic-only developer environments remain usable
    pass
else:
    settings.register_profile(
        "gate",
        max_examples=1500,
        deadline=None,
        print_blob=True,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ib_execution.calendar import TradingCalendar  # noqa: E402
from ib_execution.clock import ManualClock  # noqa: E402
from ib_execution.controller import Controller, ExecutionPolicy  # noqa: E402
from ib_execution.fake_broker import FakeBroker, Faults  # noqa: E402
from ib_execution.journal import Journal  # noqa: E402
from ib_execution.models import LinkState, Quote, SyncState, TargetPosition  # noqa: E402
from ib_execution.risk import RiskConfig, RiskEngine  # noqa: E402


# A Wednesday, mid-session, regular trading day: 2026-08-05 14:00 UTC == 10:00 ET
SESSION_START = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(SESSION_START)


@pytest.fixture
def journal(tmp_path, clock) -> Journal:
    j = Journal(tmp_path / "journal.db", clock=clock)
    yield j
    j.close()


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig(
        symbol_whitelist=("SPY",),
        strategy_whitelist=("manual_test",),
        max_position_shares=5,
        max_order_shares=10,
        max_order_notional=Decimal("20000"),
        max_daily_shares=200,
        max_orders_per_day=50,
        max_orders_per_minute=8,
    )


@pytest.fixture
def faults() -> Faults:
    return Faults()


@pytest.fixture
def broker(clock, faults) -> FakeBroker:
    return FakeBroker(clock, faults)


@pytest.fixture
def ctl(journal, broker, risk_config, clock) -> Controller:
    c = Controller(
        journal=journal,
        broker=broker,
        risk=RiskEngine(risk_config, clock),
        clock=clock,
        calendar=TradingCalendar(),
        policy=ExecutionPolicy(),
    )
    # Bring the system to a normally-operating state.
    c.on_connected(1)
    c.on_quote(quote(clock.now()))
    c.reconcile()
    assert c.link_state is LinkState.CONNECTED
    assert c.sync_state is SyncState.SYNCED
    return c


def quote(ts, bid="599.98", ask="600.02") -> Quote:
    return Quote("SPY", Decimal(bid), Decimal(ask), 500, 500, ts)


_counter = [0]


def target(qty: int, clock, strategy="manual_test", symbol="SPY", ttl_seconds=60,
           decision_id: str | None = None) -> TargetPosition:
    _counter[0] += 1
    return TargetPosition(
        strategy_id=strategy,
        symbol=symbol,
        target_quantity=qty,
        decision_id=decision_id or f"d{_counter[0]:05d}",
        valid_until=clock.now() + timedelta(seconds=ttl_seconds),
    )
