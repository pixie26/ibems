"""
Preflight. Run before every session.

Everything here is a refusal, not a warning. A check that only logs is a check
that gets ignored on the morning it matters.

    python -m ib_execution.preflight [--journal data/journal.db]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .calendar import TradingCalendar
from .clock import SystemClock
from .journal import Journal
from .config_loader import ConfigError, load_risk_config
from .risk import RiskConfig, RiskSelfTestFailed, run_self_test


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        return f"[{'PASS' if self.passed else 'FAIL'}] {self.name}: {self.detail}"


def check_risk_self_test(config: RiskConfig, clock) -> Check:
    """Invariant 21. A config hash says what loaded; only this says it works."""
    try:
        proven = run_self_test(config, clock)
        return Check("risk_self_test", True, f"{len(proven)} checks proven live")
    except RiskSelfTestFailed as exc:
        return Check("risk_self_test", False, str(exc))


def check_calendar(cal: TradingCalendar, today: date) -> Check:
    """
    A holiday table that has rolled past its review year is a silent hazard:
    a half day moves the close and a stale flatten time never fires.
    """
    years = {d.year for d in cal.holidays}
    if today.year not in years:
        return Check(
            "calendar",
            False,
            f"holiday table covers {sorted(years)} but today is {today.year}. "
            f"Update calendar.py (RUNBOOK section 5).",
        )
    plan = cal.plan(today)
    return Check("calendar", True, plan.describe())


def check_clock(local_now: datetime, broker_now: Optional[datetime],
                max_skew: float = 2.0) -> Check:
    if broker_now is None:
        return Check("clock", False, "no broker time available; cannot verify skew")
    skew = abs((local_now - broker_now).total_seconds())
    return Check(
        "clock",
        skew <= max_skew,
        f"skew {skew:.3f}s (max {max_skew}s)" + ("" if skew <= max_skew else " — check NTP"),
    )


def check_fsync(journal: Journal, p99_threshold_ms: float = 250.0) -> Check:
    """
    The journal writer thread keeps fsync off the event loop, but the controller
    still blocks on durability. Measure it; do not assume it.
    """
    for _ in range(20):
        journal.commit(
            __import__("ib_execution.models", fromlist=["EventType"]).EventType.FSYNC_LATENCY_SAMPLE,
            {"probe": True},
        )
    s = journal.fsync_stats()
    p99 = s.get("p99_ms", 0.0)
    return Check(
        "fsync_latency",
        p99 <= p99_threshold_ms,
        f"p50={s.get('p50_ms', 0):.1f}ms p99={p99:.1f}ms max={s.get('max_ms', 0):.1f}ms",
    )


def check_no_credentials_in_repo(root: Path) -> Check:
    """
    Cheap, and it has caught real incidents.

    Paper credentials get copied to live accounts more often than anyone admits;
    that is the actual attack path, not a compromised paper account.
    """
    suspicious = []
    # Tokens are assembled at runtime so this scanner does not match its own
    # source, and so it keeps working if the file is ever renamed.
    tokens = ["pass" + "word:", "pass" + "wd:", "ib_" + "password", "pass" + "word ="]
    self_name = Path(__file__).name
    for pattern in ("*.yml", "*.yaml", "*.json", "*.py", "*.env"):
        for p in root.rglob(pattern):
            if any(part in {".git", ".venv", "__pycache__", "data"} for part in p.parts):
                continue
            if p.name == self_name:
                continue
            try:
                text = p.read_text(errors="ignore").lower()
            except OSError:
                continue
            for token in tokens:
                if token in text:
                    suspicious.append(f"{p.name}:{token}")
    return Check(
        "no_credentials_in_repo",
        not suspicious,
        "clean" if not suspicious else f"possible credentials: {suspicious}",
    )


def run(
    journal_path: str = "data/preflight.db",
    risk_config_path: str = "config/risk.yml",
    root: Optional[Path] = None,
    broker_now: Optional[datetime] = None,
) -> int:
    """Production preflight. Missing real config or broker time is a refusal."""
    clock = SystemClock()
    root = root or Path(__file__).resolve().parents[2]
    Path(journal_path).parent.mkdir(parents=True, exist_ok=True)
    journal = Journal(journal_path, clock=clock)
    checks: list[Check] = []
    try:
        try:
            risk_config = load_risk_config(risk_config_path)
            checks.append(Check("risk_config", True, f"loaded {risk_config_path}"))
            self_test = check_risk_self_test(risk_config, clock)
            checks.append(self_test)
            journal.commit(
                __import__("ib_execution.models", fromlist=["EventType"]).EventType.RISK_CONFIG_LOADED,
                {"path": str(risk_config_path), "config_hash": risk_config.config_hash()},
            )
            if self_test.passed:
                journal.commit(
                    __import__("ib_execution.models", fromlist=["EventType"]).EventType.RISK_SELF_TEST_PASSED,
                    {
                        "config_hash": risk_config.config_hash(),
                        "detail": self_test.detail,
                    },
                )
        except ConfigError as exc:
            checks.append(Check("risk_config", False, str(exc)))

        checks += [
            check_calendar(TradingCalendar(), datetime.now(timezone.utc).date()),
            check_fsync(journal),
            check_no_credentials_in_repo(root),
            check_clock(clock.now(), broker_now),
        ]
    finally:
        journal.close()

    for c in checks:
        print(c)
    failed = [c for c in checks if not c.passed]
    if failed:
        print(f"\nPREFLIGHT FAILED ({len(failed)}). Do not start the engine.")
        return 1
    print("\nPREFLIGHT PASSED.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default="data/preflight.db")
    ap.add_argument("--risk-config", default="config/risk.yml")
    a = ap.parse_args()
    # Gate B2 must wire real broker time into run(); until then this CLI
    # intentionally fails the clock check rather than printing a false PASS.
    raise SystemExit(run(a.journal, a.risk_config))
