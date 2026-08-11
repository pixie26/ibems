"""
IB Gateway adapter.

    ####################################################################
    #  STATUS: UNVERIFIED.  This module has never connected to a       #
    #  Gateway. Every behavioural claim below is taken from IB         #
    #  documentation, which lags the implementation. Gate B2 exists    #
    #  precisely to replace these assumptions with measurements.       #
    ####################################################################

Do not treat anything here as tested. The Phase 0 core is tested; this is not.

Gate B2 deliverable is not "it works". It is a table:

    | documented behaviour        | observed behaviour | matches? |
    |-----------------------------|--------------------|----------|
    | orderRef survives to exec   | ?                  | ?        |
    | permId stable cross-session | ?                  | ?        |
    | 1101 vs 1102 semantics      | ?                  | ?        |
    | execId correction format    | ?                  | ?        |
    | paper fills at top of book  | ?                  | ?        |

That table is itself a deliverable, and arguably the most valuable one Gate B2
produces.

CREDENTIALS: never in this file, never in config, never in the repo. Environment
only. If a password has ever been pasted into a chat window, a ticket, or a
commit, it is burned -- rotate it.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from .broker_protocol import BrokerCallbacks, BrokerRejected, BrokerSendUncertain
from .models import (
    BrokerOrder,
    BrokerSnapshot,
    Execution,
    OrderIntent,
    Quote,
    Side,
)


# Fixed, dedicated client ids. Never 0 for the trading process: clientId 0 binds
# orders placed manually in TWS, and binding can trigger cancel/resubmit, which
# would cost queue position on an order we did not create.
CLIENT_ID_EXECUTION = 31
CLIENT_ID_EMERGENCY = 32
CLIENT_ID_RECORDER = 33

# IB documents roughly 50 messages/sec before it may disconnect the client.
# A single-symbol platform can only approach this via a runaway loop, so the
# token bucket is a runaway breaker, not a throughput control.
MAX_MESSAGES_PER_SECOND = 45


class AdapterCallbackFailed(RuntimeError):
    """An ib_async callback failed and the adapter state is no longer trustworthy."""


class IbConfig:
    """Connection parameters. Credentials come from the environment only."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,             # 4002 paper gateway, 4001 live gateway
        client_id: int = CLIENT_ID_EXECUTION,
        account: Optional[str] = None,
        read_only: bool = False,
        request_timeout_seconds: float = 10.0,
    ):
        if request_timeout_seconds <= 0:
            raise ValueError(
                "request_timeout_seconds must be positive; ib_async reads 0 as wait forever"
            )
        self.host = host
        self.port = port
        self.client_id = client_id
        self.read_only = read_only
        self.account = account or os.environ.get("IB_ACCOUNT")
        self.request_timeout_seconds = float(request_timeout_seconds)

    @staticmethod
    def credentials_note() -> str:
        return (
            "IB Gateway authenticates at the Gateway process, not through the API. "
            "This platform never handles a username or password. Supply them to "
            "IBC/Gateway via environment variables or a file outside the repo."
        )


class TokenBucket:
    """Runaway breaker. Not throughput management."""

    def __init__(self, rate_per_sec: float = MAX_MESSAGES_PER_SECOND, burst: int = 20):
        self.rate = rate_per_sec
        self.capacity = burst
        self.tokens = float(burst)
        self.last: Optional[float] = None

    def allow(self, monotonic_now: float) -> bool:
        if self.last is None:
            self.last = monotonic_now
        elapsed = monotonic_now - self.last
        self.last = monotonic_now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


# Gate B2 requirement: snapshot() must set is_stable=True only after a measured
# broker-side completion/watermark barrier across positions, open orders and
# executions. A simple sequential read is not sufficient.

class IbAdapter:
    """
    Implements Broker on top of ib_async.

    ib_async is the maintained fork; ib_insync was archived in 2024. Import is
    deferred so the Phase 0 core stays importable, and testable, on a machine
    with no IB dependency at all.
    """

    def __init__(self, config: IbConfig, clock):
        self.config = config
        self.clock = clock
        # Gate B2 must receive AsyncControllerBridge here, never Controller
        # directly, so durable journal waits cannot block the IB event loop.
        self._cb: Optional[BrokerCallbacks] = None
        self._ib = None
        self._contract = None
        self._bucket = TokenBucket()
        self._ref_to_trade: dict[str, object] = {}
        self._connection_epoch = 0
        self._fatal_callback_error: Optional[str] = None
        self._event_wrappers: list[object] = []

    @property
    def order_capable(self) -> bool:
        """A read-write IB connection must pass ExecutionHost's platform gate."""

        return not self.config.read_only

    # -- lifecycle --------------------------------------------------------

    def connect(self) -> None:
        try:
            from ib_async import IB  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "ib_async not installed. `pip install ib_async`. "
                "The Phase 0 core does not need it; only this adapter does."
            ) from exc

        self._ib = IB()
        # ib_async defaults RequestTimeout to 0, which means an omitted
        # completion callback blocks the only event loop forever. The adapter,
        # not its test harness, owns this deadline.
        from .quote_recorder import enforce_request_deadline  # noqa: PLC0415

        enforce_request_deadline(self._ib, self.config.request_timeout_seconds)
        self._ib.connect(
            self.config.host,
            self.config.port,
            clientId=self.config.client_id,
            readonly=self.config.read_only,
        )
        self._connection_epoch += 1
        self._wire_events()

        # Clock authority. Refuse to start on excessive skew rather than
        # discovering it later as mysterious stale-target rejections.
        from .clock import assert_clock_sane  # noqa: PLC0415
        assert_clock_sane(self.clock.now(), self.server_time(), max_skew_seconds=2.0)

        if self._cb:
            self._cb.on_connected(self._connection_epoch)

    def _wire_events(self) -> None:
        """
        TODO(Gate B2): every handler must be wrapped so an exception cannot be
        swallowed by the event loop. A lost state transition leaves the system
        looking alive while its state is wrong. The controller's _guarded() is
        the pattern; this must not bypass it.
        """
        ib = self._ib
        assert ib is not None
        bindings = (
            (ib.orderStatusEvent, "orderStatus", self._on_order_status),
            (ib.execDetailsEvent, "execDetails", self._on_exec_details),
            (ib.commissionReportEvent, "commissionReport", self._on_commission),
            (ib.errorEvent, "error", self._on_error),
            (ib.disconnectedEvent, "disconnected", self._on_disconnected),
            (ib.pendingTickersEvent, "pendingTickers", self._on_tickers),
        )
        self._event_wrappers.clear()
        for event, name, handler in bindings:
            wrapped = self._guard_event(name, handler)
            self._event_wrappers.append(wrapped)
            event += wrapped

    def _guard_event(self, name, handler):
        """Turn eventkit-swallowed exceptions into a latched fatal state."""

        def guarded(*args):
            try:
                handler(*args)
            except BaseException as exc:
                if self._fatal_callback_error is None:
                    self._fatal_callback_error = (
                        f"IB {name} callback raised {type(exc).__name__}: {exc}"
                    )
                if self._cb is not None:
                    self._cb.on_disconnected(self._fatal_callback_error)

        return guarded

    def raise_if_failed(self) -> None:
        if self._fatal_callback_error is not None:
            raise AdapterCallbackFailed(self._fatal_callback_error)

    # -- Broker protocol --------------------------------------------------

    def register(self, callbacks: BrokerCallbacks) -> None:
        self._cb = callbacks

    def is_connected(self) -> bool:
        return bool(
            self._fatal_callback_error is None
            and self._ib
            and self._ib.isConnected()
        )

    def server_time(self) -> datetime:
        self.raise_if_failed()
        assert self._ib is not None
        t = self._ib.reqCurrentTime()
        return t.astimezone(timezone.utc) if t.tzinfo else t.replace(tzinfo=timezone.utc)

    def place_order(self, intent: OrderIntent) -> int:
        """
        UNVERIFIED.

        The distinction that matters: BrokerRejected means we know no order
        exists; BrokerSendUncertain means we do not know. Mapping a real IB
        failure onto the wrong one is the single most dangerous bug this
        adapter can contain, because "uncertain" misread as "rejected" produces
        a duplicate position.

        When in doubt, raise BrokerSendUncertain. Reconciliation is cheap; a
        duplicate is not.
        """
        raise NotImplementedError(
            "Gate B2. Requires: LMT order construction, orderRef assignment, "
            "token bucket, and a documented mapping from every IB error code to "
            "either BrokerRejected or BrokerSendUncertain."
        )

    def cancel_order(self, order_ref: str) -> None:
        raise NotImplementedError("Gate B2")

    def snapshot(self) -> BrokerSnapshot:
        """
        UNVERIFIED.

        Must use reqAllOpenOrders() (reads without binding) rather than a
        subscription, plus positions, plus today's executions. This is how an
        externally-placed order is detected without accidentally adopting it.
        """
        raise NotImplementedError("Gate B2")

    # -- event handlers (all UNVERIFIED) ----------------------------------

    def _on_order_status(self, trade) -> None:
        raise NotImplementedError("Gate B2")

    def _on_exec_details(self, trade, fill) -> None:
        """
        Note: execution and commission arrive separately, and the fee can be
        much later. "Execution known, fee unknown" is a legal intermediate
        state, not an error. Recent IB versions renamed commission fields to
        commissionAndFees; map whatever ib_async exposes onto our FEE_RECEIVED.
        """
        raise NotImplementedError("Gate B2")

    def _on_commission(self, trade, fill, report) -> None:
        raise NotImplementedError("Gate B2")

    def _on_error(self, reqId, errorCode, errorString, contract) -> None:
        """
        1100 connectivity lost, 1101 restored WITH subscription loss,
        1102 restored with subscriptions intact.

        1101 is the one that matters: the socket is back and the account state
        is not trustworthy. Treat as normal lifecycle, not as an exception --
        the Gateway is designed to restart daily.
        """
        raise NotImplementedError("Gate B2")

    def _on_disconnected(self) -> None:
        if self._cb:
            self._cb.on_disconnected("ib disconnected")

    def _on_tickers(self, tickers) -> None:
        raise NotImplementedError("Gate B2")
