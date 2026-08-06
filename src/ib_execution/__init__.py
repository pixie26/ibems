"""
ib_execution -- a thin, single-account, single-writer execution platform.

Design intent (see docs/SPEC.md, frozen):
  - Strategies emit TargetPosition. Nothing else. Ever.
  - The platform owns positions, orders, risk, reconciliation and EOD.
  - Broker is the authority on facts; the journal is the authority on meaning.
  - Safety over liveness: when we do not know, we stop.

Phase status:
  Phase 0 core (models, journal, controller, fake_broker, risk, auditor)
      -- reviewed in-process prototype; Gate B1 not yet passed.
  IB-facing modules (ib_adapter, quote_recorder, emergency_flatten)
      -- UNVERIFIED skeletons. They have never touched a Gateway.
"""

from .models import (  # noqa: F401
    Execution,
    FlattenReason,
    LinkState,
    MissReason,
    OperatingMode,
    OrderIntent,
    OrderState,
    OrderType,
    Quote,
    Side,
    SyncState,
    TargetPosition,
)
from .clock import ManualClock, SystemClock  # noqa: F401
from .journal import Journal  # noqa: F401
from .risk import RiskConfig, RiskEngine, run_self_test  # noqa: F401
from .controller import Controller, ExecutionPolicy  # noqa: F401
from .fake_broker import FakeBroker, Faults  # noqa: F401
from .calendar import TradingCalendar  # noqa: F401
from .auditor import JournalAuditor  # noqa: F401

__version__ = "0.1.5.dev0"
