"""
Pre-trade risk.

The production failure of a risk engine is almost never "the risk engine
crashed". It is "the risk engine was configured to allow it". So this module
does three things beyond the obvious checks:

1. HARD BOUNDS IN CODE. Config cannot raise a limit past a compiled-in ceiling.
   A fat-fingered YAML cannot authorise a position the platform was never
   designed to survive.

2. STARTUP SELF-TEST (invariant 21). At boot we construct orders that MUST be
   rejected and verify they are. If any is allowed, the process refuses to
   start. A config hash proves what was loaded; a self-test proves it works.

3. RUNAWAY BREAKERS. The disaster case for an automated system is not one bad
   order, it is a loop emitting ten thousand. Per-minute order count and daily
   cumulative shares/notional matter more than every other check combined.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from .models import (
    OrderIntent,
    OrderType,
    Quote,
    RiskRejection,
    Side,
    stable_hash,
)


# Compiled-in ceilings. Config may lower these, never raise them.
# Deliberately tight: V1 is a data-collection experiment, not a deployment.
HARD_MAX_POSITION_SHARES = 100
HARD_MAX_ORDER_SHARES = 200
HARD_MAX_ORDER_NOTIONAL = Decimal("200000")
HARD_MAX_DAILY_SHARES = 2000
HARD_MAX_DAILY_NOTIONAL = Decimal("1000000")
HARD_MAX_ORDERS_PER_DAY = 200
HARD_MAX_ORDERS_PER_MINUTE = 10


@dataclass
class RiskConfig:
    symbol_whitelist: tuple[str, ...] = ("SPY",)
    strategy_whitelist: tuple[str, ...] = ("manual_test", "intraday_momentum_spy")

    max_position_shares: int = 5
    max_order_shares: int = 10   # >= 2 x max_position_shares, or reversals break
    max_order_notional: Decimal = Decimal("5000")

    max_daily_shares: int = 200
    max_daily_notional: Decimal = Decimal("100000")
    max_orders_per_day: int = 50
    max_orders_per_minute: int = 4

    allow_short: bool = True

    max_quote_age_seconds: float = 5.0
    max_spread_bps: float = 25.0
    max_limit_deviation_bps: float = 30.0

    # Invariant 19 is numeric, not a checkbox. The risk engine evaluates the
    # stressed overnight loss on every risk-increasing order.
    overnight_gap_stress_pct: Decimal = Decimal("0.15")
    max_overnight_loss: Decimal = Decimal("500")


    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Schema + hard bound check. Runs before anything else at boot."""
        errs: list[str] = []
        if not isinstance(self.allow_short, bool):
            errs.append("allow_short must be boolean")
        if self.max_position_shares <= 0:
            errs.append("max_position_shares must be > 0")
        if self.max_position_shares > HARD_MAX_POSITION_SHARES:
            errs.append(
                f"max_position_shares {self.max_position_shares} exceeds hard bound "
                f"{HARD_MAX_POSITION_SHARES}"
            )
        if self.max_order_shares <= 0:
            errs.append("max_order_shares must be > 0")
        if self.max_order_shares > HARD_MAX_ORDER_SHARES:
            errs.append(f"max_order_shares exceeds hard bound {HARD_MAX_ORDER_SHARES}")
        if self.max_order_shares < 2 * self.max_position_shares:
            # A full reversal (+N -> -N) requires 2N shares in ONE order. If
            # max_order_shares is below that, every flip is silently rejected by
            # risk and the strategy simply never reverses -- with no error, just
            # a growing pile of RISK_BLOCKED misses. Found by scripts/demo.py.
            errs.append(
                f"max_order_shares ({self.max_order_shares}) < 2 x max_position_shares "
                f"({self.max_position_shares}): a full reversal would be unexecutable"
            )
        if self.max_order_notional <= 0 or self.max_order_notional > HARD_MAX_ORDER_NOTIONAL:
            errs.append(f"max_order_notional must be in (0, {HARD_MAX_ORDER_NOTIONAL}]")
        if self.max_daily_shares <= 0 or self.max_daily_shares > HARD_MAX_DAILY_SHARES:
            errs.append(f"max_daily_shares must be in (0, {HARD_MAX_DAILY_SHARES}]")
        if self.max_daily_notional <= 0 or self.max_daily_notional > HARD_MAX_DAILY_NOTIONAL:
            errs.append(f"max_daily_notional must be in (0, {HARD_MAX_DAILY_NOTIONAL}]")
        if self.max_orders_per_day <= 0 or self.max_orders_per_day > HARD_MAX_ORDERS_PER_DAY:
            errs.append(f"max_orders_per_day must be in (0, {HARD_MAX_ORDERS_PER_DAY}]")
        if (
            self.max_orders_per_minute <= 0
            or self.max_orders_per_minute > HARD_MAX_ORDERS_PER_MINUTE
        ):
            errs.append(
                f"max_orders_per_minute must be in (0, {HARD_MAX_ORDERS_PER_MINUTE}]"
            )
        if not self.symbol_whitelist:
            errs.append("symbol_whitelist empty")
        if not self.strategy_whitelist:
            errs.append("strategy_whitelist empty")
        if self.max_quote_age_seconds <= 0:
            errs.append("max_quote_age_seconds must be > 0")
        if self.max_spread_bps <= 0:
            errs.append("max_spread_bps must be > 0")
        if self.max_limit_deviation_bps <= 0:
            errs.append("max_limit_deviation_bps must be > 0")
        if self.overnight_gap_stress_pct <= 0 or self.overnight_gap_stress_pct > 1:
            errs.append("overnight_gap_stress_pct must be in (0, 1]")
        if self.max_overnight_loss <= 0:
            errs.append("max_overnight_loss must be > 0")
        if errs:
            raise ValueError("risk config invalid:\n  - " + "\n  - ".join(errs))

    def config_hash(self) -> str:
        return stable_hash(asdict(self))


@dataclass
class _DayCounters:
    day: Optional[str] = None
    orders: int = 0
    shares: int = 0
    notional: Decimal = Decimal(0)
    minute_stamps: list[datetime] = field(default_factory=list)

    def roll(self, now: datetime) -> None:
        d = now.date().isoformat()
        if self.day != d:
            self.day = d
            self.orders = 0
            self.shares = 0
            self.notional = Decimal(0)
            self.minute_stamps = []


class RiskEngine:
    def __init__(self, config: RiskConfig, clock):
        self.config = config
        self.clock = clock
        self._counters = _DayCounters()
        self._config_hash = config.config_hash()

    @property
    def config_hash(self) -> str:
        return self._config_hash

    # -- checks -----------------------------------------------------------

    def check(
        self,
        intent: OrderIntent,
        *,
        current_position: int,
        quote: Optional[Quote],
        is_closing: bool = False,
    ) -> dict:
        """Raise on rejection; otherwise return durable, auditor-recomputable evidence."""
        now = self.clock.now()
        self._counters.roll(now)
        c = self.config

        if intent.symbol not in c.symbol_whitelist:
            raise RiskRejection("symbol_whitelist", intent.symbol)
        if intent.strategy_id not in c.strategy_whitelist:
            raise RiskRejection("strategy_whitelist", intent.strategy_id)

        if intent.quantity <= 0:
            raise RiskRejection("quantity", "must be positive")
        if intent.quantity > c.max_order_shares:
            raise RiskRejection(
                "max_order_shares", f"{intent.quantity} > {c.max_order_shares}"
            )

        resulting = current_position + intent.signed_quantity
        if abs(resulting) > c.max_position_shares:
            raise RiskRejection(
                "max_position_shares",
                f"resulting {resulting} exceeds {c.max_position_shares}",
            )
        if resulting < 0 and not c.allow_short:
            raise RiskRejection("allow_short", "short not permitted")

        # Runaway breakers -- the ones that actually matter.
        if self._counters.orders + 1 > c.max_orders_per_day:
            raise RiskRejection("max_orders_per_day", str(self._counters.orders))
        recent = [t for t in self._counters.minute_stamps if now - t < timedelta(minutes=1)]
        if len(recent) + 1 > c.max_orders_per_minute:
            raise RiskRejection("max_orders_per_minute", str(len(recent)))
        if self._counters.shares + intent.quantity > c.max_daily_shares:
            raise RiskRejection("max_daily_shares", str(self._counters.shares))

        # Quote-dependent checks. A closing order under duress may proceed on a
        # stale quote -- but it is journalled, not silently waived.
        if quote is not None:
            age = quote.age_seconds(now)
            if age > c.max_quote_age_seconds and not is_closing:
                raise RiskRejection("quote_stale", f"{age:.1f}s")
            if quote.mid > 0:
                spread_bps = float(quote.spread / quote.mid) * 10_000
                if spread_bps > c.max_spread_bps and not is_closing:
                    raise RiskRejection("max_spread_bps", f"{spread_bps:.1f}")
            if intent.limit_price is not None and quote.mid > 0:
                dev = abs(intent.limit_price - quote.mid) / quote.mid
                dev_bps = float(dev) * 10_000
                cap = c.max_limit_deviation_bps * (3 if is_closing else 1)
                if dev_bps > cap:
                    raise RiskRejection("limit_collar", f"{dev_bps:.1f} bps from mid")
            notional = intent.limit_price or quote.mid
        else:
            if not is_closing:
                raise RiskRejection("no_quote", "quote required for opening orders")
            notional = intent.limit_price or Decimal(0)

        order_notional = notional * intent.quantity
        if order_notional > c.max_order_notional:
            raise RiskRejection("max_order_notional", str(order_notional))
        if self._counters.notional + order_notional > c.max_daily_notional:
            raise RiskRejection(
                "max_daily_notional",
                f"{self._counters.notional + order_notional} > {c.max_daily_notional}",
            )

        # Invariant 19: prove overnight survivability numerically whenever the
        # action leaves non-zero exposure and a quote is available.
        if resulting != 0 and quote is not None:
            stressed_loss = abs(resulting) * quote.mid * c.overnight_gap_stress_pct
            if stressed_loss > c.max_overnight_loss:
                raise RiskRejection(
                    "overnight_stress",
                    f"stress loss {stressed_loss} > budget {c.max_overnight_loss}",
                )

        reference_price = quote.mid if quote is not None else Decimal(0)
        stressed_loss = abs(resulting) * reference_price * c.overnight_gap_stress_pct
        return {
            "config_hash": self.config_hash,
            "is_closing": bool(is_closing),
            "resulting_position": resulting,
            "reference_price": str(reference_price),
            "overnight_gap_stress_pct": str(c.overnight_gap_stress_pct),
            "stressed_loss": str(stressed_loss),
            "max_overnight_loss": str(c.max_overnight_loss),
            "max_position_shares": c.max_position_shares,
            "max_order_shares": c.max_order_shares,
            "max_order_notional": str(c.max_order_notional),
            "max_orders_per_minute": c.max_orders_per_minute,
            "max_orders_per_day": c.max_orders_per_day,
            "max_daily_shares": c.max_daily_shares,
            "max_daily_notional": str(c.max_daily_notional),
        }

    def record_sent(self, intent: OrderIntent, price: Decimal) -> None:
        now = self.clock.now()
        self._counters.roll(now)
        self._counters.orders += 1
        self._counters.shares += intent.quantity
        self._counters.notional += price * intent.quantity
        self._counters.minute_stamps.append(now)

    def restore_from_events(self, events) -> None:
        """Rebuild daily runaway counters after a process restart."""
        intents: dict[str, dict] = {}
        seen_sent: set[str] = set()
        self._counters = _DayCounters()
        for ev in events:
            if ev.event_type.value == "ORDER_INTENT_COMMITTED" and ev.intent_id:
                intents[ev.intent_id] = ev.payload
            elif ev.event_type.value == "SEND_ATTEMPT_STARTED" and ev.intent_id:
                if ev.intent_id in seen_sent:
                    continue
                seen_sent.add(ev.intent_id)
                payload = intents.get(ev.intent_id, {})
                ts = ev.ts_utc
                self._counters.roll(ts)
                qty = int(payload.get("quantity", ev.payload.get("quantity", 0)))
                px_raw = payload.get("limit_price", ev.payload.get("price"))
                px = Decimal(str(px_raw)) if px_raw not in (None, "None") else Decimal(0)
                self._counters.orders += 1
                self._counters.shares += qty
                self._counters.notional += px * qty
                self._counters.minute_stamps.append(ts)

    def snapshot(self) -> dict:
        return {
            "day": self._counters.day,
            "orders": self._counters.orders,
            "shares": self._counters.shares,
            "notional": str(self._counters.notional),
            "max_daily_notional": str(self.config.max_daily_notional),
            "config_hash": self._config_hash,
        }


# --------------------------------------------------------------------------
# Startup self-test (invariant 21)
# --------------------------------------------------------------------------


class RiskSelfTestFailed(RuntimeError):
    """A must-reject order was allowed. The process must not start."""


def _probe(symbol, strategy, side=Side.BUY, qty=1, px="600") -> OrderIntent:
    """A probe order. Symbol and strategy are required, never defaulted.

    They used to default to ``"SPY"`` and ``"manual_test"``, which quietly made
    the self-test a test of one particular configuration. See ``run_self_test``.
    """
    return OrderIntent(
        intent_id="selftest",
        decision_id="selftest",
        strategy_id=strategy,
        symbol=symbol,
        side=side,
        quantity=qty,
        order_type=OrderType.MARKETABLE_LIMIT,
        limit_price=Decimal(px),
        target_quantity=qty,
        position_snapshot=0,
        working_snapshot=0,
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=1),
        risk_config_hash="selftest",
        execution_policy_hash="selftest",
        order_ref="selftest",
    )


def _absent(values: tuple[str, ...], stem: str) -> str:
    """A name guaranteed not to be in ``values``, for the whitelist probes."""
    candidate = f"__not_a_real_{stem}__"
    while candidate in values:
        candidate += "x"
    return candidate


def run_self_test(config: RiskConfig, clock) -> list[str]:
    """
    Construct orders that MUST be rejected; verify each one is rejected
    **by the control it is meant to exercise**.

    Returns the list of check names proven live. Raises if any probe passes.
    A config hash tells you what was loaded. Only this tells you it works.

    TWO THINGS THIS GETS RIGHT THAT IT PREVIOUSLY DID NOT
    -----------------------------------------------------
    1.  The baseline symbol and strategy come from ``config``, not from
        hardcoded ``"SPY"`` / ``"manual_test"`` defaults. With those defaults,
        any configuration that did not happen to whitelist both crashed out of
        this function -- and therefore out of ``Controller.__init__`` -- with
        ``RiskRejection: symbol_whitelist: SPY``. A production config for a
        different instrument could not construct a controller at all.

    2.  Each probe must be rejected by *its own* check. Previously any
        ``RiskRejection`` counted, so with a config whitelisting only ``QQQ``
        the ``limit_collar`` probe was rejected by ``symbol_whitelist`` and
        recorded as proof that the collar was live. That is the worse of the
        two failures: a self-test whose entire purpose is to demonstrate that
        specific controls fire, accepting the wrong control firing as proof.
    """
    now = clock.now()
    if not config.symbol_whitelist or not config.strategy_whitelist:
        raise RiskSelfTestFailed(
            "the risk configuration has an empty symbol or strategy whitelist; "
            "the must-reject self-test has no admissible baseline order"
        )
    symbol = config.symbol_whitelist[0]
    strategy = config.strategy_whitelist[0]
    good_quote = Quote(symbol, Decimal("599.98"), Decimal("600.02"), 100, 100, now)

    def probe(**over) -> OrderIntent:
        return _probe(**{"symbol": symbol, "strategy": strategy, **over})

    cases: list[tuple[str, OrderIntent, dict]] = [
        (
            "symbol_whitelist",
            probe(symbol=_absent(config.symbol_whitelist, "symbol")),
            {"current_position": 0, "quote": good_quote},
        ),
        (
            "strategy_whitelist",
            probe(strategy=_absent(config.strategy_whitelist, "strategy")),
            {"current_position": 0, "quote": good_quote},
        ),
        (
            "max_order_shares",
            probe(qty=config.max_order_shares + 1),
            {"current_position": 0, "quote": good_quote},
        ),
        (
            "max_position_shares",
            probe(qty=1),
            {"current_position": config.max_position_shares, "quote": good_quote},
        ),
        (
            "quote_stale",
            probe(),
            {
                "current_position": 0,
                "quote": Quote(
                    symbol,
                    Decimal("599.98"),
                    Decimal("600.02"),
                    100,
                    100,
                    now - timedelta(seconds=config.max_quote_age_seconds + 10),
                ),
            },
        ),
        (
            "max_spread_bps",
            probe(),
            {
                "current_position": 0,
                "quote": Quote(symbol, Decimal("590"), Decimal("610"), 100, 100, now),
            },
        ),
        (
            "limit_collar",
            probe(px="700"),
            {"current_position": 0, "quote": good_quote},
        ),
        (
            "no_quote",
            probe(),
            {"current_position": 0, "quote": None},
        ),
    ]

    proven: list[str] = []
    failures: list[str] = []
    for name, intent, kwargs in cases:
        engine = RiskEngine(config, clock)  # fresh: no counter pollution
        try:
            engine.check(intent, **kwargs)
        except RiskRejection as exc:
            if exc.check == name:
                proven.append(name)
            else:
                # Rejected, but by a different control: this probe demonstrates
                # nothing about `name`, and counting it would be a false pass.
                failures.append(f"{name} (rejected by {exc.check} instead)")
        else:
            failures.append(name)

    # Runaway breaker needs a stateful engine.
    engine = RiskEngine(config, clock)
    tripped = False
    for _ in range(config.max_orders_per_minute + 2):
        p = probe()
        try:
            engine.check(p, current_position=0, quote=good_quote)
        except RiskRejection as exc:
            if "orders_per_minute" in exc.check:
                tripped = True
                break
            # The baseline order must be admissible under its own config.
            failures.append(f"max_orders_per_minute (baseline rejected by {exc.check})")
            break
        engine.record_sent(p, Decimal("600"))
    if tripped:
        proven.append("max_orders_per_minute")
    elif not any(f.startswith("max_orders_per_minute") for f in failures):
        failures.append("max_orders_per_minute")

    if failures:
        raise RiskSelfTestFailed(
            "risk checks did NOT reject orders they must reject: "
            + ", ".join(failures)
            + " -- refusing to start"
        )
    return proven
