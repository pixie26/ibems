from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ib_execution.ib_adapter import AdapterCallbackFailed, IbAdapter, IbConfig


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def emit(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class _FakeIB:
    def __init__(self):
        self.RequestTimeout = 0
        self.orderStatusEvent = _Event()
        self.execDetailsEvent = _Event()
        self.commissionReportEvent = _Event()
        self.errorEvent = _Event()
        self.disconnectedEvent = _Event()
        self.pendingTickersEvent = _Event()
        self.connected = False

    def connect(self, *_args, **_kwargs):
        assert self.RequestTimeout > 0
        self.connected = True

    def reqCurrentTime(self):
        return datetime.now(timezone.utc)

    def isConnected(self):
        return self.connected


def test_adapter_owns_a_positive_deadline_before_connect(monkeypatch):
    monkeypatch.setitem(sys.modules, "ib_async", SimpleNamespace(IB=_FakeIB))
    clock = SimpleNamespace(now=lambda: datetime.now(timezone.utc))
    adapter = IbAdapter(IbConfig(read_only=True, request_timeout_seconds=7), clock)
    adapter.connect()
    assert adapter._ib.RequestTimeout == 7.0


def test_adapter_refuses_ib_asyncs_infinite_timeout_semantics():
    with pytest.raises(ValueError, match="wait forever"):
        IbConfig(request_timeout_seconds=0)


def test_only_read_write_adapter_instances_declare_order_capability():
    clock = SimpleNamespace(now=lambda: datetime.now(timezone.utc))
    assert IbAdapter(IbConfig(read_only=False), clock).order_capable
    assert not IbAdapter(IbConfig(read_only=True), clock).order_capable


def test_callback_exception_is_latched_even_when_dispatch_continues(monkeypatch):
    monkeypatch.setitem(sys.modules, "ib_async", SimpleNamespace(IB=_FakeIB))
    clock = SimpleNamespace(now=lambda: datetime.now(timezone.utc))
    adapter = IbAdapter(IbConfig(read_only=True), clock)
    disconnected = []
    adapter.register(SimpleNamespace(on_connected=lambda *_: None, on_disconnected=disconnected.append))
    adapter.connect()
    adapter._ib.orderStatusEvent.emit(object())
    assert disconnected and "NotImplementedError" in disconnected[0]
    assert not adapter.is_connected()
    with pytest.raises(AdapterCallbackFailed, match="orderStatus callback"):
        adapter.raise_if_failed()
