"""Strict configuration loading.

YAML is an operational input, not documentation. Unknown keys, missing
environment variables, and type coercion errors are startup failures. Risk
configuration is immutable for the lifetime of a process; a change requires a
restart and produces a new config hash on every intent.
"""

from __future__ import annotations

import os
import re
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .risk import RiskConfig

_ENV = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")
_DECIMAL_FIELDS = {
    "max_order_notional",
    "max_daily_notional",
    "overnight_gap_stress_pct",
    "max_overnight_loss",
}
_TUPLE_FIELDS = {"symbol_whitelist", "strategy_whitelist"}


class ConfigError(ValueError):
    pass


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        m = _ENV.match(value)
        if m:
            name = m.group(1)
            if name not in os.environ:
                raise ConfigError(f"required environment variable is not set: {name}")
            return os.environ[name]
        return value
    if isinstance(value, list):
        return [_expand(x) for x in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config does not exist: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"top-level config must be a mapping: {p}")
    return _expand(raw)


def load_risk_config(path: str | Path) -> RiskConfig:
    raw = load_yaml(path)
    allowed = {f.name for f in fields(RiskConfig)}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unknown risk config key(s): {unknown}")

    cooked = dict(raw)
    for name in _DECIMAL_FIELDS:
        if name in cooked:
            cooked[name] = Decimal(str(cooked[name]))
    for name in _TUPLE_FIELDS:
        if name in cooked:
            value = cooked[name]
            if not isinstance(value, list):
                raise ConfigError(f"{name} must be a YAML list")
            cooked[name] = tuple(str(x) for x in value)
    try:
        return RiskConfig(**cooked)
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc
