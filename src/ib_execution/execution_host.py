"""
The execution host process.

    ####################################################################
    #  This module exists so that `fatal_shutdown_requested` is        #
    #  consumed by something.  Until now it was a bool that the core   #
    #  set, a watchdog read out of a status file, and no process ever  #
    #  acted on -- the fail-closed decision had no exit path.          #
    ####################################################################

Gate B1.3a is "a fatal journal failure ends the process with a non-zero exit
and the supervisor does not silently take over". Gate B1.3b is the harder
half: "and the next start still refuses to trade". This host is where both
become true, and the startup order below is the whole safety argument:

    1.  calendar coverage self-test    -- refuse a session we cannot bound
    2.  journal writer ownership       -- refuse to be the second writer
    3.  durable fatal fence check      -- refuse to launder a previous fault
    4.  restore from journal           -- durable HALT and residual survive
    5.  connect + reconcile            -- broker truth before any write
    6.  retire the fence               -- only now, and only if reconciled

Steps 1-3 all happen *before* the broker is touched. A process that will not
be allowed to trade must never open a session that could.

WHAT THIS IS NOT
----------------
Not a supervisor, not a restart policy, and not a strategy loop. It owns
process lifecycle and exit codes. The supervisor lives outside (see
``deploy/``), and Gate B1.3a is only satisfied when that configuration is
frozen alongside the code -- proving the child exits non-zero says nothing if
production is configured to restart it.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .broker_protocol import Broker
from .calendar import CalendarCoverageError, TradingCalendar
from .clock import Clock, SystemClock
from .controller import Controller, ExecutionPolicy
from .fatal_fence import FatalFence, FenceStillRaised
from .journal import Journal, JournalOwnershipError
from .journal_witness import JournalWitness, WitnessViolation
from .risk import RiskEngine
from .watchdog import write_status

# Exit codes are part of the operational contract: a supervisor, and the Gate
# B1 harness, both branch on them. Never reuse one for a different meaning.
EXIT_OK = 0
EXIT_FATAL_SHUTDOWN = 10      # the engine fenced itself; do not restart blindly
EXIT_NOT_OWNER = 11           # another execution host owns this journal
EXIT_FENCED = 12              # a durable fatal fence from a previous run
EXIT_CALENDAR = 13            # the trading calendar does not cover this date
EXIT_STARTUP = 14             # any other refusal before the broker was touched
EXIT_WITNESS = 15             # the journal lost evidence that authorised a broker write


class HostStartupRefused(RuntimeError):
    """A pre-broker refusal, carrying the exit code the process should use."""

    def __init__(self, code: int, detail: str):
        self.code = code
        super().__init__(detail)


@dataclass
class HostConfig:
    journal_path: Path
    fence_path: Path
    status_path: Path
    # Gate B1.6. Defaults to a sibling of the fence, which is the point: it has
    # to survive the journal's volume, exactly like the fence does.
    witness_path: Optional[Path] = None
    require_separate_fence_domain: bool = True
    heartbeat_seconds: float = 1.0


class ExecutionHost:
    """Owns the process: startup gates, the run loop, and the exit code."""

    def __init__(
        self,
        config: HostConfig,
        broker_factory: Callable[[], Broker],
        risk: RiskEngine,
        clock: Optional[Clock] = None,
        calendar: Optional[TradingCalendar] = None,
        policy: Optional[ExecutionPolicy] = None,
        alert: Optional[Callable[[str, str], None]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ):
        self.config = config
        self.broker_factory = broker_factory
        self.risk = risk
        self.clock = clock or SystemClock()
        self.sleeper = sleeper or time.sleep
        self.calendar = calendar or TradingCalendar()
        self.policy = policy or ExecutionPolicy()
        self.alert = alert or (lambda level, msg: print(f"[{level}] {msg}", flush=True))
        self.controller: Optional[Controller] = None
        self.journal: Optional[Journal] = None
        self.fence = FatalFence(
            config.fence_path,
            config.journal_path,
            require_separate_domain=config.require_separate_fence_domain,
        )
        self.witness = JournalWitness(
            config.witness_path
            or config.fence_path.with_name("journal-witness.json")
        )
        self._stop = False
        self._heartbeat_failed = False

    # -- startup gates ---------------------------------------------------

    def _gate_calendar(self) -> dict[str, Any]:
        try:
            return self.calendar.self_test(self.clock.now())
        except CalendarCoverageError as exc:
            raise HostStartupRefused(EXIT_CALENDAR, str(exc)) from exc

    def _gate_fence_configuration(self) -> None:
        try:
            self.fence.verify_domain()
        except Exception as exc:  # noqa: BLE001 - FenceDomainError and OSError alike
            raise HostStartupRefused(EXIT_STARTUP, str(exc)) from exc

    def _gate_ownership(self) -> Journal:
        try:
            return Journal(self.config.journal_path, clock=self.clock)
        except JournalOwnershipError as exc:
            raise HostStartupRefused(EXIT_NOT_OWNER, str(exc)) from exc

    def _gate_fence(self) -> None:
        try:
            self.fence.require_clear()
        except FenceStillRaised as exc:
            raise HostStartupRefused(EXIT_FENCED, str(exc)) from exc

    def _gate_witness(self, journal: Journal) -> None:
        """Gate B1.6, and the reason it runs before the broker is constructed.

        If the journal no longer holds the event that authorised the last
        broker write, an order may be live at the broker with no local record.
        Connecting first and reconciling would be reasoning from a journal we
        have just proved incomplete.
        """
        try:
            self.witness.verify(journal)
        except WitnessViolation as exc:
            self.fence.raise_fence(f"journal witness violation: {exc}")
            raise HostStartupRefused(EXIT_WITNESS, str(exc)) from exc

    def start(self) -> Controller:
        """Run every gate, then build the controller. Broker untouched until the end."""
        calendar_state = self._gate_calendar()
        self._gate_fence_configuration()
        # Fence before ownership: a fenced host should not even briefly hold
        # the journal, so that an operator tool can open it while diagnosing.
        self._gate_fence()
        self.journal = self._gate_ownership()
        try:
            self._gate_witness(self.journal)
        except HostStartupRefused:
            self.journal.close()
            self.journal = None
            raise

        self.controller = Controller(
            journal=self.journal,
            broker=self.broker_factory(),
            risk=self.risk,
            clock=self.clock,
            calendar=self.calendar,
            policy=self.policy,
            alert=self.alert,
            fence=self.fence,
            witness=self.witness,
        )
        self.controller.restore_from_journal()
        self.alert(
            "INFO",
            f"execution host started: pid={os.getpid()} "
            f"session={calendar_state['today']} "
            f"mode={self.controller.operating_mode.value}",
        )
        return self.controller

    # -- run loop --------------------------------------------------------

    def request_stop(self, *_args) -> None:
        self._stop = True

    def _heartbeat(self) -> None:
        """Best-effort. The status file is telemetry, not a control.

        It usually lives beside the journal, so the case where writing it fails
        is overwhelmingly "the journal volume is full" -- exactly the case
        where the exit code matters most. An unhandled ``OSError`` here used to
        kill the host with exit 1, which is not in the documented exit-code
        table the supervisor branches on. Found by the Gate B1.4 drill on a
        real full volume, not by fault injection.

        A stale status file is not a silent failure: the watchdog treats a
        stopped heartbeat as critical, and the CRITICAL alert has already been
        emitted through a channel that does not touch this disk.
        """
        controller = self.controller
        assert controller is not None
        try:
            write_status(self.config.status_path, controller.status())
        except OSError as exc:
            if not self._heartbeat_failed:
                self._heartbeat_failed = True
                self.alert("CRITICAL", f"cannot write the status file ({exc}); heartbeat is stale")

    def run_once(self) -> Optional[int]:
        """One supervision tick. Returns an exit code when the host must stop.

        This is the consumer that was missing. ``fatal_shutdown_requested`` is
        a statement that the durable path is gone, and the only correct
        response to it is to stop the process -- not to log it, and not to
        keep serving callbacks on state we no longer believe.

        The fatal check comes before the heartbeat: nothing may run ahead of
        the decision to stop.
        """
        controller = self.controller
        assert controller is not None
        if controller.fatal_shutdown_requested:
            detail = controller.journal_failure or "fatal shutdown requested"
            if controller.fence_write_failed:
                self.alert(
                    "CRITICAL",
                    "exiting non-zero WITHOUT a durable fence "
                    f"({controller.fence_write_failed}); a restart will not be "
                    "blocked automatically. Reconcile the account by hand.",
                )
            else:
                self.alert(
                    "CRITICAL",
                    f"exiting non-zero with a durable fence raised: {detail}",
                )
            return EXIT_FATAL_SHUTDOWN
        if self._stop:
            return EXIT_OK
        self._heartbeat()
        return None

    def run(self, ticks: Optional[int] = None) -> int:
        """Supervise until a fatal shutdown, a stop request, or ``ticks`` elapse."""
        assert self.controller is not None
        completed = 0
        while ticks is None or completed < ticks:
            code = self.run_once()
            if code is not None:
                return code
            completed += 1
            self.sleeper(self.config.heartbeat_seconds)
        return EXIT_OK

    def close(self) -> None:
        if self.journal is not None:
            self.journal.close()
            self.journal = None

    # -- fence retirement ------------------------------------------------

    def retire_fence_after_reconciliation(self, *, reconciled: bool) -> None:
        """Phase two of retirement, callable only once the account is explained.

        Deliberately not automatic on startup: the whole value of the gap
        between raising and retiring is that somebody looked at broker truth.
        """
        self.fence.retire(reconciled=reconciled)


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="IB execution host")
    ap.add_argument("--journal", required=True)
    ap.add_argument(
        "--fence",
        required=True,
        help="durable fatal fence path; MUST be on a different volume from --journal",
    )
    ap.add_argument("--status", default="status.json")
    ap.add_argument(
        "--witness",
        default=None,
        help="journal witness path; defaults to a sibling of --fence, and must "
             "be on a different volume from --journal for the same reason",
    )
    ap.add_argument(
        "--allow-shared-fence-volume",
        action="store_true",
        help="testing only: skip the separate-failure-domain check",
    )
    ap.add_argument("--ticks", type=int, default=None, help="stop after N supervision ticks")
    return ap


def main(  # pragma: no cover - exercised by tests/test_execution_host.py subprocesses
    argv: Optional[list[str]] = None,
    *,
    broker_factory: Optional[Callable[[], Broker]] = None,
    risk: Optional[RiskEngine] = None,
) -> int:
    ns = build_argparser().parse_args(argv)
    if broker_factory is None or risk is None:
        # There is no default broker on purpose. Wiring a real IB adapter here
        # would make it possible to start a trading process by accident, and
        # the adapter is unverified until Gate B2.
        print(
            "execution_host has no default broker: the IB trading adapter is "
            "unverified until Gate B2. Supply one programmatically.",
            file=sys.stderr,
        )
        return EXIT_STARTUP

    host = ExecutionHost(
        HostConfig(
            journal_path=Path(ns.journal),
            fence_path=Path(ns.fence),
            status_path=Path(ns.status),
            witness_path=Path(ns.witness) if ns.witness else None,
            require_separate_fence_domain=not ns.allow_shared_fence_volume,
        ),
        broker_factory=broker_factory,
        risk=risk,
    )
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, host.request_stop)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    try:
        host.start()
    except HostStartupRefused as exc:
        print(f"execution host refusing to start: {exc}", file=sys.stderr)
        return exc.code

    try:
        return host.run(ticks=ns.ticks)
    finally:
        host.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
