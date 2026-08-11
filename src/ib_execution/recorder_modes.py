"""Explicit market-data retention policies for execution and research.

The three modes are deliberately named in code and in every evidence manifest.
No caller should have to infer whether a file is complete tick data from its
filename or from a row count.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any


class DataMode(str, Enum):
    EXECUTION_MINIMAL = "execution_minimal"
    EVIDENCE_SAMPLED = "evidence_sampled"
    RESEARCH_FULL = "research_full"


@dataclass
class CapturePolicy:
    """Decide which already-handled market events enter the raw event log."""

    mode: DataMode | str = DataMode.RESEARCH_FULL
    bidask_sample_interval_seconds: float = 1.0
    bidask_on_price_change: bool = True
    decision_pre_window_seconds: float = 30.0
    decision_window_seconds: float = 30.0

    def __post_init__(self) -> None:
        self.mode = DataMode(self.mode)
        if self.bidask_sample_interval_seconds <= 0:
            raise ValueError("bidask sample interval must be positive")
        if self.decision_pre_window_seconds < 0 or self.decision_window_seconds < 0:
            raise ValueError("decision windows must not be negative")
        self._last_bidask_mono = -math.inf
        self._last_bidask: tuple[float, float] | None = None
        self._full_fidelity_until_mono = -math.inf

    def open_decision_window(self, now_mono: float, seconds: float | None = None) -> None:
        """Keep full BidAsk fidelity through a decision-adjacent window."""

        duration = self.decision_window_seconds if seconds is None else float(seconds)
        if duration < 0:
            raise ValueError("decision window must not be negative")
        self._full_fidelity_until_mono = max(
            self._full_fidelity_until_mono,
            float(now_mono) + duration,
        )

    def should_persist(
        self,
        event_type: str,
        *,
        now_mono: float,
        bid: float | None = None,
        ask: float | None = None,
    ) -> bool:
        if event_type == "SYSTEM":
            return True
        if self.mode is DataMode.EXECUTION_MINIMAL:
            return False
        if self.mode is DataMode.RESEARCH_FULL or event_type != "BID_ASK":
            return True

        quote = None if bid is None or ask is None else (float(bid), float(ask))
        price_changed = self.bidask_on_price_change and quote != self._last_bidask
        interval_elapsed = (
            float(now_mono) - self._last_bidask_mono
            >= self.bidask_sample_interval_seconds
        )
        in_decision_window = float(now_mono) <= self._full_fidelity_until_mono
        keep = price_changed or interval_elapsed or in_decision_window
        if keep:
            self._last_bidask_mono = float(now_mono)
            self._last_bidask = quote
        return keep

    def manifest(self) -> dict[str, Any]:
        if self.mode is DataMode.EXECUTION_MINIMAL:
            bidask_rule = "none"
        elif self.mode is DataMode.RESEARCH_FULL:
            bidask_rule = "all"
        else:
            bidask_rule = "interval_or_price_change_or_decision_window"
        return {
            "mode": self.mode.value,
            "all_last": "all" if self.mode is not DataMode.EXECUTION_MINIMAL else "none",
            "bar_5s": "all" if self.mode is not DataMode.EXECUTION_MINIMAL else "none",
            "bidask": bidask_rule,
            "bidask_sample_interval_seconds": self.bidask_sample_interval_seconds,
            "bidask_on_price_change": self.bidask_on_price_change,
            "decision_pre_window_seconds": self.decision_pre_window_seconds,
            "decision_window_seconds": self.decision_window_seconds,
        }
