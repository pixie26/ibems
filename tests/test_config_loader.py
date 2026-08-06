from decimal import Decimal

import pytest

from ib_execution.config_loader import ConfigError, load_risk_config


def test_loads_actual_numeric_risk_config(tmp_path):
    p = tmp_path / "risk.yml"
    p.write_text(
        """
symbol_whitelist: [SPY]
strategy_whitelist: [manual_test]
max_position_shares: 5
max_order_shares: 10
max_order_notional: 5000
max_daily_shares: 200
max_daily_notional: 100000
max_orders_per_day: 50
max_orders_per_minute: 4
overnight_gap_stress_pct: 0.15
max_overnight_loss: 500
"""
    )
    c = load_risk_config(p)
    assert c.symbol_whitelist == ("SPY",)
    assert c.max_daily_notional == Decimal("100000")
    assert c.overnight_gap_stress_pct == Decimal("0.15")


def test_unknown_config_key_fails_closed(tmp_path):
    p = tmp_path / "risk.yml"
    p.write_text("max_position_sharez: 99\n")
    with pytest.raises(ConfigError, match="unknown"):
        load_risk_config(p)


@pytest.mark.parametrize(
    "body, message",
    [
        ("max_position_shares: 0\n", "max_position_shares"),
        ("max_daily_shares: -1\n", "max_daily_shares"),
        ("allow_short: 1\n", "allow_short must be boolean"),
        ("max_orders_per_minute: 11\n", "max_orders_per_minute"),
    ],
)
def test_unsafe_config_values_fail_closed(tmp_path, body, message):
    p = tmp_path / "risk.yml"
    p.write_text(body)
    with pytest.raises(ConfigError, match=message):
        load_risk_config(p)
