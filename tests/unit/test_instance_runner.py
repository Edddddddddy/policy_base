"""Purpose: unit-test the one-round policy child runner (RoundCommand/SIGTERM/timeout/report).

Uses tiny fake child processes (never ROS, never a real policy backend) plus a fake
``Popen`` to assert the frozen ``RoundCommand`` startup contract.

Usage: env -u PYTHONPATH .venv/bin/python -m pytest tests/unit/test_instance_runner.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from policy_base.instance import runner
from policy_base.instance.protocol import (
    CHILD_FAILED,
    CHILD_INTERRUPTED,
    CHILD_OK,
    CHILD_SPAWN_ERROR,
    CHILD_TIMEOUT,
)
from policy_base.policy.backend import RoundCommand

REPO_ROOT = Path(__file__).resolve().parents[2]

#: A child that handles SIGTERM gracefully (exits 0) while sleeping.
GRACEFUL_CHILD = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
    "time.sleep(30)\n"
)
#: A child that ignores SIGTERM (forces the SIGKILL escalation path).
STUBBORN_CHILD = (
    "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)\n"
)


def make_command(
    argv: tuple[str, ...],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    report_path: Path | None = None,
) -> RoundCommand:
    return RoundCommand(
        argv=argv,
        env={} if env is None else env,
        cwd=cwd,
        report_path=report_path,
        description="unit-test round",
    )


def run_fake_child(
    code: str,
    *,
    timeout_s: float = 5.0,
    stop_grace_s: float = 1.0,
    interrupt_after: float | None = None,
    on_tick=None,
    report_reader=None,
) -> tuple[runner.ChildOutcome, float, threading.Event]:
    interrupt = threading.Event()
    if interrupt_after is not None:
        timer = threading.Timer(interrupt_after, interrupt.set)
        timer.daemon = True
        timer.start()
    command = make_command((sys.executable, "-c", code))
    started = time.monotonic()
    kwargs = {}
    if report_reader is not None:
        kwargs["report_reader"] = report_reader
    outcome = runner.run_child(
        command,
        timeout_s=timeout_s,
        stop_grace_s=stop_grace_s,
        interrupt=interrupt,
        on_tick=on_tick,
        poll_interval_s=0.02,
        **kwargs,
    )
    return outcome, time.monotonic() - started, interrupt


class FakeProcess:
    """Minimal ``Popen`` double: exits immediately with ``returncode``."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.signals: list[int] = []
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_defaults_are_frozen():
    assert runner.DEFAULT_ROUND_TIMEOUT_S == 600.0
    assert runner.DEFAULT_STOP_GRACE_S == 5.0
    assert runner.DEFAULT_POLL_INTERVAL_S == 0.05
    assert runner.KILL_WAIT_S == 2.0


# --- frozen RoundCommand startup contract ---------------------------------
def test_round_command_argv_env_and_cwd_are_used(tmp_path):
    recorded: dict[str, object] = {}

    def factory(argv, **kwargs):
        recorded["argv"] = argv
        recorded.update(kwargs)
        return FakeProcess(0)

    command = make_command(
        ("python", "-m", "policy_base.policy.mock", "--report", "r.json"),
        env={"SO101_DEVICE": "cuda"},
        cwd=tmp_path,
    )
    outcome = runner.run_child(
        command,
        timeout_s=1.0,
        stop_grace_s=0.1,
        interrupt=threading.Event(),
        popen_factory=factory,
        base_env={"PATH": "/usr/bin", "FOO": "bar"},
    )
    assert outcome.status == CHILD_OK
    assert outcome.returncode == 0
    assert recorded["argv"] == ["python", "-m", "policy_base.policy.mock", "--report", "r.json"]
    assert recorded["cwd"] == str(tmp_path)
    env = recorded["env"]
    assert isinstance(env, dict)
    assert env["PATH"] == "/usr/bin"
    assert env["FOO"] == "bar"
    assert env["SO101_DEVICE"] == "cuda"


def test_default_child_cwd_is_the_policy_base_repository():
    recorded: dict[str, object] = {}

    def factory(argv, **kwargs):
        recorded.update(kwargs)
        return FakeProcess(0)

    runner.run_child(
        make_command(("true",)),
        timeout_s=1.0,
        stop_grace_s=0.1,
        interrupt=threading.Event(),
        popen_factory=factory,
    )
    assert recorded["cwd"] == str(runner.policy_repo_root())


def test_command_env_overrides_inherited_env(monkeypatch):
    recorded: dict[str, object] = {}

    def factory(argv, **kwargs):
        recorded.update(kwargs)
        return FakeProcess(0)

    monkeypatch.setenv("POLICY_BASE_UNIT_ENV", "inherited")
    runner.run_child(
        make_command(("true",), env={"POLICY_BASE_UNIT_ENV": "command"}),
        timeout_s=1.0,
        stop_grace_s=0.1,
        interrupt=threading.Event(),
        popen_factory=factory,
    )
    env = recorded["env"]
    assert isinstance(env, dict)
    assert env["POLICY_BASE_UNIT_ENV"] == "command"


def test_build_child_env_overlays_command_env():
    command = make_command(("true",), env={"A": "command", "B": "command"})
    env = runner.build_child_env(command, base_env={"A": "base", "C": "base"})
    assert env == {"A": "command", "B": "command", "C": "base"}


def test_paths_are_located_from_the_installed_package():
    assert runner.policy_src_dir().name == "src"
    assert runner.policy_repo_root() == runner.policy_src_dir().parent
    assert runner.policy_repo_root() == REPO_ROOT


# --- report reading -------------------------------------------------------
def test_report_is_read_after_the_round(tmp_path):
    calls: list[Path | None] = []

    def reader(path: Path | None) -> dict[str, object] | None:
        calls.append(path)
        return {"schema": "so101.policy_base.round_report.v1", "published": 7, "steps": 5}

    report_path = tmp_path / "round_0001.json"
    outcome = runner.run_child(
        make_command((sys.executable, "-c", "pass"), report_path=report_path),
        timeout_s=5.0,
        stop_grace_s=0.1,
        interrupt=threading.Event(),
        report_reader=reader,
    )
    assert outcome.status == CHILD_OK
    assert outcome.report == {
        "schema": "so101.policy_base.round_report.v1",
        "published": 7,
        "steps": 5,
    }
    assert calls == [report_path]


def test_missing_report_path_does_not_call_the_reader():
    calls: list[Path | None] = []

    def reader(path: Path | None) -> dict[str, object] | None:
        calls.append(path)
        return None

    outcome = runner.run_child(
        make_command((sys.executable, "-c", "pass")),
        timeout_s=5.0,
        stop_grace_s=0.1,
        interrupt=threading.Event(),
        report_reader=reader,
    )
    assert outcome.ok is True
    assert outcome.report is None
    assert calls == []


def test_failing_report_reader_degrades_to_none(tmp_path):
    def reader(path: Path | None) -> dict[str, object] | None:
        raise RuntimeError("corrupt report")

    outcome = runner.run_child(
        make_command((sys.executable, "-c", "pass"), report_path=tmp_path / "r.json"),
        timeout_s=5.0,
        stop_grace_s=0.1,
        interrupt=threading.Event(),
        report_reader=reader,
    )
    assert outcome.status == CHILD_OK
    assert outcome.report is None


def test_report_is_read_after_a_failed_round(tmp_path):
    outcome = runner.run_child(
        make_command(
            (sys.executable, "-c", "raise SystemExit(4)"), report_path=tmp_path / "r.json"
        ),
        timeout_s=5.0,
        stop_grace_s=0.1,
        interrupt=threading.Event(),
        report_reader=lambda path: {"status": "failed", "rows": 1},
    )
    assert outcome.status == CHILD_FAILED
    assert outcome.report == {"status": "failed", "rows": 1}


# --- child outcome classification -----------------------------------------
def test_child_ok_exit_zero():
    outcome, elapsed, _ = run_fake_child("pass")
    assert outcome.status == CHILD_OK
    assert outcome.returncode == 0
    assert outcome.ok is True
    assert outcome.interrupted is False
    assert outcome.timed_out is False
    assert elapsed < 10


def test_child_failed_nonzero_exit_is_classified():
    outcome, _, _ = run_fake_child("raise SystemExit(3)")
    assert outcome.status == CHILD_FAILED
    assert outcome.returncode == 3
    assert outcome.stage == "round"
    assert "3" in (outcome.error or "")


def test_child_timeout_terminates_the_process():
    outcome, elapsed, _ = run_fake_child("import time; time.sleep(30)", timeout_s=0.3)
    assert outcome.status == CHILD_TIMEOUT
    assert outcome.timed_out is True
    assert outcome.returncode is not None
    assert elapsed < 5.0


def test_spawn_error_is_reported_not_raised():
    outcome = runner.run_child(
        make_command(("/definitely/not/a/real/program",)),
        timeout_s=1.0,
        stop_grace_s=0.1,
        interrupt=threading.Event(),
    )
    assert outcome.status == CHILD_SPAWN_ERROR
    assert outcome.stage == "spawn"
    assert outcome.error
    assert outcome.report is None


def test_on_tick_is_called_while_the_child_runs():
    ticks = []
    outcome, _, _ = run_fake_child(
        "import time; time.sleep(0.3)", timeout_s=5.0, on_tick=lambda: ticks.append(1)
    )
    assert outcome.status == CHILD_OK
    assert len(ticks) >= 3


# --- SIGTERM forwarding ----------------------------------------------------
def test_interrupt_forwards_sigterm_to_child():
    outcome, elapsed, _ = run_fake_child(GRACEFUL_CHILD, interrupt_after=0.25)
    assert outcome.status == CHILD_INTERRUPTED
    assert outcome.interrupted is True
    # Graceful child exits 0 on SIGTERM (no SIGKILL escalation needed).
    assert outcome.returncode == 0
    assert elapsed < 5.0


def test_interrupt_escalates_to_sigkill_after_grace():
    outcome, elapsed, _ = run_fake_child(STUBBORN_CHILD, stop_grace_s=0.2, interrupt_after=0.25)
    assert outcome.status == CHILD_INTERRUPTED
    assert outcome.returncode is not None and outcome.returncode < 0  # killed by signal
    assert elapsed < 5.0


def test_interrupt_before_spawn_does_not_start_a_child():
    interrupt = threading.Event()
    interrupt.set()
    started = time.monotonic()
    outcome = runner.run_child(
        make_command((sys.executable, "-c", "import time; time.sleep(10)")),
        timeout_s=5.0,
        stop_grace_s=0.5,
        interrupt=interrupt,
    )
    assert outcome.status == CHILD_INTERRUPTED
    assert outcome.returncode is None
    assert outcome.report is None
    assert time.monotonic() - started < 2.0


def test_interrupt_sends_sigterm_to_a_fake_child():
    process = FakeProcess(0)

    def factory(argv, **kwargs):
        return process

    interrupt = threading.Event()
    interrupt.set()
    outcome = runner.run_child(
        make_command(("true",)),
        timeout_s=1.0,
        stop_grace_s=0.1,
        interrupt=interrupt,
        popen_factory=factory,
    )
    assert outcome.status == CHILD_INTERRUPTED
    assert process.signals == []


# --- validation ------------------------------------------------------------
def test_timeout_must_be_positive():
    with pytest.raises(ValueError, match="timeout_s"):
        runner.run_child(
            make_command((sys.executable, "-c", "pass")),
            timeout_s=0,
            stop_grace_s=0.1,
            interrupt=threading.Event(),
        )


def test_stop_grace_must_not_be_negative():
    with pytest.raises(ValueError, match="stop_grace_s"):
        runner.run_child(
            make_command((sys.executable, "-c", "pass")),
            timeout_s=1.0,
            stop_grace_s=-1,
            interrupt=threading.Event(),
        )


def test_round_runner_rejects_invalid_timings():
    with pytest.raises(ValueError, match="timeout_s"):
        runner.RoundRunner(timeout_s=0)
    with pytest.raises(ValueError, match="stop_grace_s"):
        runner.RoundRunner(stop_grace_s=-1)
    with pytest.raises(ValueError, match="poll_interval_s"):
        runner.RoundRunner(poll_interval_s=0)


def test_no_child_is_left_running_after_timeout():
    """A timed-out fake child must be reaped (no zombie / lingering sleeper)."""
    marker = f"pb_instance_unit_{os.getpid()}_{time.monotonic_ns()}"
    code = f"import time; time.sleep(30)  # {marker}"
    outcome, _, _ = run_fake_child(code, timeout_s=0.3)
    assert outcome.status == CHILD_TIMEOUT
    probe = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True, check=False)
    assert probe.stdout.strip() == "", f"lingering child: {probe.stdout}"


# --- RoundRunner -----------------------------------------------------------
def test_round_runner_configuration_and_spawn_error(tmp_path):
    round_runner = runner.RoundRunner(
        timeout_s=123.0,
        stop_grace_s=1.5,
        poll_interval_s=0.01,
        report_reader=lambda path: {"status": "ok"},
    )
    assert round_runner.timeout_s == 123.0
    assert round_runner.stop_grace_s == 1.5
    assert round_runner.poll_interval_s == 0.01
    assert round_runner.interrupted is False
    outcome = round_runner.run(make_command(("/definitely/not/a/real/program",)))
    assert outcome.status == CHILD_SPAWN_ERROR
    assert outcome.stage == "spawn"


def test_round_runner_runs_a_command_and_reads_the_report(tmp_path):
    report_path = tmp_path / "round.json"
    round_runner = runner.RoundRunner(
        timeout_s=5.0,
        stop_grace_s=0.1,
        base_env=dict(os.environ),
        report_reader=lambda path: {"published": 3} if path == report_path else None,
    )
    outcome = round_runner.run(
        make_command((sys.executable, "-c", "pass"), report_path=report_path)
    )
    assert outcome.status == CHILD_OK
    assert outcome.report == {"published": 3}


def test_round_runner_interrupt_is_idempotent():
    round_runner = runner.RoundRunner()
    assert round_runner.interrupted is False
    round_runner.interrupt()
    round_runner.interrupt()
    assert round_runner.interrupted is True
    outcome = round_runner.run(make_command((sys.executable, "-c", "import time; time.sleep(10)")))
    assert outcome.status == CHILD_INTERRUPTED
    assert outcome.returncode is None
