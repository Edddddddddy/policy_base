"""One-round policy subprocess runner for the policy_base instance.

The instance entry never imports the policy/ACT stack: every round is a fresh child
process whose frozen launch contract (:class:`policy_base.policy.backend.RoundCommand`)
is built by :func:`policy_base.policy.backend.build_round_command`. This module owns the
subprocess mechanics: env overlay, single-round timeout, SIGTERM forwarding (SIGTERM,
then SIGKILL after a grace period), the interrupt latch and outcome classification. It
has **no ROS imports** and never imports torch/lerobot.

Semantics mirror the frozen rule_base instance runner (``run_child`` / ``RoundRunner``)
so the IM can treat both repositories identically; the only change is the injection
point: rule_base built the child argv itself, policy_base receives a
:class:`~policy_base.policy.backend.RoundCommand` and starts exactly that command.

The round report written by the child is read back through
:func:`policy_base.policy.backend.read_round_report` after the process ends. The report
is diagnostic: the process exit code stays authoritative for pass/fail classification;
a missing/corrupt report simply yields ``ChildOutcome.report is None``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import policy_base
from policy_base.instance.protocol import (
    CHILD_FAILED,
    CHILD_INTERRUPTED,
    CHILD_OK,
    CHILD_SPAWN_ERROR,
    CHILD_TIMEOUT,
)
from policy_base.policy.backend import RoundCommand, read_round_report

#: One policy round's default budget (matches the rule_base 600 s single-round contract).
DEFAULT_ROUND_TIMEOUT_S = 600.0
DEFAULT_STOP_GRACE_S = 5.0
DEFAULT_POLL_INTERVAL_S = 0.05
KILL_WAIT_S = 2.0


class _ChildProcess(Protocol):
    """Minimal ``subprocess.Popen`` surface used by the runner (also easy to fake)."""

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def send_signal(self, sig: int) -> None: ...

    def kill(self) -> None: ...


@dataclass(frozen=True)
class ChildOutcome:
    """Classified result of one policy child round."""

    status: str
    returncode: int | None
    stage: str
    error: str | None = None
    report: dict[str, object] | None = None

    @property
    def ok(self) -> bool:
        return self.status == CHILD_OK

    @property
    def interrupted(self) -> bool:
        return self.status == CHILD_INTERRUPTED

    @property
    def timed_out(self) -> bool:
        return self.status == CHILD_TIMEOUT


def policy_src_dir() -> Path:
    """Locate this checkout's ``src`` directory (works for editable installs too)."""
    package_file = getattr(policy_base, "__file__", None)
    if package_file is None:  # pragma: no cover - namespace package corner case
        raise RuntimeError("cannot locate the policy_base package")
    return Path(package_file).resolve().parents[1]


def policy_repo_root() -> Path:
    """Repository root (= parent of ``src``), the default child working directory."""
    return policy_src_dir().parent


def build_child_env(
    command: RoundCommand,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Inherit the instance environment and overlay the frozen ``RoundCommand.env``.

    The child (mock module or finetune ``deploy/run.sh execute``) needs ``PATH`` and the
    platform's ``PYTHONPATH``/ROS environment inherited from the instance process; the
    round command only injects its own ``SO101_*`` overrides, which win on conflict.
    """
    env = dict(os.environ if base_env is None else base_env)
    env.update(command.env)
    return env


def _read_report(
    path: Path | None,
    report_reader: Callable[[Path | None], dict[str, object] | None],
) -> dict[str, object] | None:
    if path is None:
        return None
    try:
        return report_reader(path)
    except Exception:  # noqa: BLE001 - the report is diagnostic, never authoritative
        return None


def _kill(proc: _ChildProcess, wait_s: float = KILL_WAIT_S) -> None:
    try:
        proc.kill()
    except ProcessLookupError:  # pragma: no cover - already reaped
        return
    try:
        proc.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:  # pragma: no cover - uninterruptible child
        pass


def _finish(
    proc: _ChildProcess,
    *,
    status: str,
    stage: str,
    error: str | None,
    command: RoundCommand,
    report_reader: Callable[[Path | None], dict[str, object] | None],
) -> ChildOutcome:
    return ChildOutcome(
        status=status,
        returncode=proc.poll(),
        stage=stage,
        error=error,
        report=_read_report(command.report_path, report_reader),
    )


def _terminate(
    proc: _ChildProcess,
    *,
    status: str,
    stage: str,
    error: str,
    stop_grace_s: float,
    command: RoundCommand,
    report_reader: Callable[[Path | None], dict[str, object] | None],
) -> ChildOutcome:
    """SIGTERM the child, escalate to SIGKILL after ``stop_grace_s``; return outcome."""
    try:
        proc.send_signal(signal.SIGTERM)
    except ProcessLookupError:  # pragma: no cover - raced with natural exit
        pass
    try:
        proc.wait(timeout=max(0.0, stop_grace_s))
    except subprocess.TimeoutExpired:
        _kill(proc)
    return _finish(
        proc,
        status=status,
        stage=stage,
        error=error,
        command=command,
        report_reader=report_reader,
    )


def run_child(
    command: RoundCommand,
    *,
    timeout_s: float,
    stop_grace_s: float,
    interrupt: threading.Event,
    on_tick: Callable[[], None] | None = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    popen_factory: Callable[..., _ChildProcess] = subprocess.Popen,
    report_reader: Callable[[Path | None], dict[str, object] | None] = read_round_report,
    base_env: Mapping[str, str] | None = None,
) -> ChildOutcome:
    """Run one policy child round to completion, classifying its outcome.

    ``on_tick`` is called while the child runs (the entry uses it to keep spinning rclpy
    so ``stopped`` stays published and SIGTERM handlers stay responsive).
    """
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if stop_grace_s < 0:
        raise ValueError("stop_grace_s must not be negative")
    cwd = command.cwd if command.cwd is not None else policy_repo_root()
    if interrupt.is_set():
        return ChildOutcome(
            status=CHILD_INTERRUPTED,
            returncode=None,
            stage="round",
            error="interrupted before the round started",
        )
    try:
        proc = popen_factory(
            list(command.argv),
            env=build_child_env(command, base_env=base_env),
            cwd=str(cwd),
        )
    except OSError as exc:
        return ChildOutcome(
            status=CHILD_SPAWN_ERROR,
            returncode=None,
            stage="spawn",
            error=f"{type(exc).__name__}: {exc}",
        )
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            if interrupt.is_set():
                return _terminate(
                    proc,
                    status=CHILD_INTERRUPTED,
                    stage="round",
                    error="interrupted (SIGTERM/SIGINT) while the round was running",
                    stop_grace_s=stop_grace_s,
                    command=command,
                    report_reader=report_reader,
                )
            returncode = proc.poll()
            if returncode is not None:
                if returncode == 0:
                    return _finish(
                        proc,
                        status=CHILD_OK,
                        stage="round",
                        error=None,
                        command=command,
                        report_reader=report_reader,
                    )
                return _finish(
                    proc,
                    status=CHILD_FAILED,
                    stage="round",
                    error=f"child exited with code {returncode}",
                    command=command,
                    report_reader=report_reader,
                )
            if time.monotonic() >= deadline:
                return _terminate(
                    proc,
                    status=CHILD_TIMEOUT,
                    stage="timeout",
                    error=f"round exceeded the {timeout_s:g}s timeout",
                    stop_grace_s=stop_grace_s,
                    command=command,
                    report_reader=report_reader,
                )
            if on_tick is not None:
                on_tick()
            try:
                proc.wait(timeout=poll_interval_s)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if proc.poll() is None:  # pragma: no cover - defensive: never leak a child
            _kill(proc)


class RoundRunner:
    """Configure and run single-round policy children for the instance entry."""

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_ROUND_TIMEOUT_S,
        stop_grace_s: float = DEFAULT_STOP_GRACE_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        base_env: Mapping[str, str] | None = None,
        popen_factory: Callable[..., _ChildProcess] = subprocess.Popen,
        report_reader: Callable[[Path | None], dict[str, object] | None] = read_round_report,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if stop_grace_s < 0:
            raise ValueError("stop_grace_s must not be negative")
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        self.timeout_s = float(timeout_s)
        self.stop_grace_s = float(stop_grace_s)
        self.poll_interval_s = float(poll_interval_s)
        self.base_env = dict(base_env) if base_env is not None else None
        self._popen_factory = popen_factory
        self._report_reader = report_reader
        self._interrupt = threading.Event()

    @property
    def interrupted(self) -> bool:
        return self._interrupt.is_set()

    def interrupt(self) -> None:
        """Request shutdown; the run loop SIGTERMs the current child at its next poll."""
        self._interrupt.set()

    def run(
        self,
        command: RoundCommand,
        *,
        on_tick: Callable[[], None] | None = None,
    ) -> ChildOutcome:
        return run_child(
            command,
            timeout_s=self.timeout_s,
            stop_grace_s=self.stop_grace_s,
            interrupt=self._interrupt,
            on_tick=on_tick,
            poll_interval_s=self.poll_interval_s,
            popen_factory=self._popen_factory,
            report_reader=self._report_reader,
            base_env=self.base_env,
        )
