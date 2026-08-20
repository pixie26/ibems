from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _drill_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_windows_ntfs_safe_drill.py"
    spec = importlib.util.spec_from_file_location("windows_ntfs_safe_drill", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _ScriptedPath:
    def __init__(self, outcomes: list[BaseException | str]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def read_text(self, *, encoding: str) -> str:
        assert encoding == "utf-8"
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_force_kill_readback_retries_only_transient_permission_denials():
    module = _drill_module()
    clock = _Clock()
    path = _ScriptedPath([PermissionError("busy"), PermissionError("busy"), "complete"])

    text, observation = module._read_text_with_bounded_permission_retry(
        path,
        timeout=1.0,
        retry_seconds=0.01,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert text == "complete"
    assert path.calls == 3
    assert observation == {"permission_denied_retries": 2, "release_wait_ms": 20}


def test_force_kill_readback_permission_denial_remains_bounded():
    module = _drill_module()
    clock = _Clock()
    path = _ScriptedPath([PermissionError("busy")] * 3)

    with pytest.raises(PermissionError, match="busy"):
        module._read_text_with_bounded_permission_retry(
            path,
            timeout=0.02,
            retry_seconds=0.01,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert path.calls == 3
    assert clock.sleeps == [0.01, 0.01]


def test_force_kill_readback_does_not_retry_other_failures():
    module = _drill_module()
    clock = _Clock()
    path = _ScriptedPath([ValueError("not a sharing collision")])

    with pytest.raises(ValueError, match="not a sharing collision"):
        module._read_text_with_bounded_permission_retry(
            path,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert path.calls == 1
    assert clock.sleeps == []
