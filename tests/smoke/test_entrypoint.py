"""Purpose: smoke-test the instance entry CLI contract and the no-ROS failure path.

Runs the real ``python -m policy_base.instance.entry`` process (offline, no ROS graph) and
checks the declared exit codes / JSON events; the mock round report is exercised through
the real policy backend + runner without any ROS or hardware.

Usage: env -u PYTHONPATH .venv/bin/python -m pytest tests/smoke/test_entrypoint.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace

from policy_base.instance import entry
from policy_base.instance.config import load_instance_config
from policy_base.instance.runner import RoundRunner
from policy_base.policy.backend import (
    build_round_command,
    read_round_report,
    summarize_round_report,
)

ROOT = Path(__file__).resolve().parents[2]
MOCK_CONFIG = ROOT / "configs/pick_place/mock_ros2_env.yaml"
LIVE_CONFIG = ROOT / "configs/pick_place/live_ros2_env.yaml"

INSTANCE_ENV = {
    "INFERENCE_NAMESPACE": "",
    "DOMAIN_ID": "229",
    "STATION_TYPE": "pick_place",
    "CLIENT_ID": "client-1",
    "LEASE_ID": "7",
    "INFERENCE_ID": "3",
}
CLEARED_ENV = {
    "PYTHONPATH",
    "INFERENCE_NAMESPACE",
    "DOMAIN_ID",
    "ROS_DOMAIN_ID",
    "STATION_TYPE",
    "CLIENT_ID",
    "LEASE_ID",
    "INFERENCE_ID",
}


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    process_env = {key: value for key, value in os.environ.items() if key not in CLEARED_ENV}
    if env is not None:
        process_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "policy_base.instance.entry", *args],
        cwd=ROOT,
        env=process_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def json_events(*outputs: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for output in outputs:
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("{"):
                events.append(json.loads(line))
    return events


def test_cli_help_lists_the_type1_contract_options():
    result = run_cli("--help")
    assert result.returncode == 0, result.stderr
    for option in (
        "--config",
        "--dry-run",
        "--enable-motion",
        "--confirm-real",
        "--warmup-timeout",
        "--round-timeout",
        "--hold-hz",
        "--stop-grace",
    ):
        assert option in result.stdout
    assert "policy_base ACT instance entry" in result.stdout


def test_missing_config_argument_exits_2():
    result = run_cli()
    assert result.returncode == 2
    assert "--config" in result.stderr


def test_missing_config_file_exits_2_with_config_stage():
    result = run_cli("--config", str(ROOT / "does-not-exist.yaml"), env=INSTANCE_ENV)
    assert result.returncode == 2, result.stderr
    events = json_events(result.stdout, result.stderr)
    failed = next(event for event in events if event["event"] == "instance_failed")
    assert failed["stage"] == "config"


def test_invalid_config_file_exits_2_with_config_stage(tmp_path):
    bad = tmp_path / "bad-instance.yaml"
    bad.write_text("schema: totally-wrong.v1\n", encoding="utf-8")
    result = run_cli("--config", str(bad), env=INSTANCE_ENV)
    assert result.returncode == 2, result.stderr
    events = json_events(result.stdout, result.stderr)
    failed = next(event for event in events if event["event"] == "instance_failed")
    assert failed["stage"] == "config"
    assert "schema" in str(failed["error"])


def test_without_ros_the_mock_config_exits_2_with_ros2_init_stage():
    """No rclpy in the venv: the entry must report exit 2 + instance_failed(ros2_init)."""
    result = run_cli("--config", str(MOCK_CONFIG), "--dry-run", env=INSTANCE_ENV)
    assert result.returncode == 2, result.stderr
    events = json_events(result.stdout, result.stderr)
    starting = next(event for event in events if event["event"] == "instance_starting")
    assert starting["backend"] == "mock"
    assert starting["dry_run"] is True
    failed = events[-1]
    assert failed["event"] == "instance_failed"
    assert failed["stage"] == "ros2_init"
    assert "rclpy" in str(failed["error"])


def test_without_ros_the_live_config_exits_2_with_ros2_init_stage():
    result = run_cli("--config", str(LIVE_CONFIG), env=INSTANCE_ENV)
    assert result.returncode == 2, result.stderr
    events = json_events(result.stdout, result.stderr)
    starting = next(event for event in events if event["event"] == "instance_starting")
    assert starting["backend"] == "finetune"
    assert events[-1]["stage"] == "ros2_init"


def test_entry_module_import_does_not_pull_ros_or_rule_base():
    code = (
        "import sys\n"
        "import policy_base.instance.entry  # noqa: F401\n"
        "assert 'rclpy' not in sys.modules, 'entry must not import rclpy at module import time'\n"
        "assert 'torch' not in sys.modules, 'instance entry must not import torch'\n"
        "assert 'rule_base' not in sys.modules, 'policy_base must not import rule_base'\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_entrypoint_main_is_declared_in_pyproject():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["so101-policy-base"] == "policy_base.instance.entry:main"


def test_both_declared_configs_load_with_the_real_loaders():
    mock = load_instance_config(MOCK_CONFIG)
    assert mock.policy.backend == "mock"
    assert mock.policy.allow_platform_execution is False
    assert mock.ros2.prefix == "/arm"
    assert mock.ros2.joint_states_topic == "/arm/joint_states"
    assert mock.ros2.joint_command_topic == "/arm/joint_command"

    live = load_instance_config(LIVE_CONFIG)
    assert live.policy.backend == "finetune"
    assert live.policy.allow_platform_execution is True
    assert Path(live.policy.checkpoint or "") == ROOT / "models/act-20261117-act-wrist-smoke"
    assert live.policy.finetune is not None
    assert live.policy.finetune.repo == Path("/home/ubuntu/yayyrobotics/finetune")
    assert live.policy.finetune.deploy_script == "deploy/run.sh"
    assert live.policy.finetune.runtime_config == "configs/runtime_execute.yaml"
    assert live.policy.finetune.device == "cuda"
    assert len(live.policy.finetune.commit or "") == 40


def test_mock_round_report_is_readable_through_the_runner(tmp_path):
    """Real mock child subprocess -> report file -> backend.read_round_report/summarize."""
    config = load_instance_config(MOCK_CONFIG)
    report_path = tmp_path / "round_0001.json"
    command = build_round_command(config.policy, report_path=report_path, dry_run=True)
    runner = RoundRunner(timeout_s=60.0, stop_grace_s=5.0)
    outcome = runner.run(command)
    assert outcome.ok is True, outcome.error
    report = read_round_report(report_path)
    assert report is not None
    assert report["schema"] == "so101.policy_base.round_report.v1"
    assert report["backend"] == "mock"
    assert report["status"] == "ok"
    assert isinstance(report["published"], int) and int(report["published"]) > 0
    summary = summarize_round_report(report)
    assert summary["backend"] == "mock"
    assert summary["steps"] == 30
    assert summary["published"] == 30
    assert outcome.report is not None
    assert outcome.report["published"] == report["published"]


def test_parse_args_defaults_are_the_frozen_protocol():
    args = entry.parse_args(["--config", str(MOCK_CONFIG)])
    assert args.dry_run is False
    assert args.enable_motion is False
    assert args.confirm_real is False
    assert args.warmup_timeout is None
    assert args.round_timeout is None
    assert args.hold_hz is None
    assert args.stop_grace == 5.0


# --- no-ROS end-to-end mock round through entry.main (fake rclpy modules) -----------------
class _FakeBool:
    def __init__(self, *, data: bool = False) -> None:
        self.data = data


class _FakeJointState:
    def __init__(self) -> None:
        self.header = SimpleNamespace(stamp=None)
        self.name: list[str] = []
        self.position: list[float] = []


class _FakePublisher:
    def __init__(self, msg_type, topic, qos) -> None:
        self.topic = topic
        self.published: list[object] = []

    def publish(self, message) -> None:
        self.published.append(message)


class _FakeSubscription:
    def __init__(self, msg_type, topic, callback, qos) -> None:
        self.topic = topic
        self.callback = callback


class _FakeTimer:
    def __init__(self, period_s, callback) -> None:
        self.period_s = period_s
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _FakeClock:
    def now(self):
        return SimpleNamespace(to_msg=lambda: "stamp")


class _FakeNode:
    instances: list["_FakeNode"] = []

    def __init__(self, name: str = "fake_node") -> None:
        self.name = name
        self.publishers: list[_FakePublisher] = []
        self.subscriptions: list[_FakeSubscription] = []
        self.timers: list[_FakeTimer] = []
        self.destroyed = False
        _FakeNode.instances.append(self)

    def create_publisher(self, msg_type, topic, qos) -> _FakePublisher:
        publisher = _FakePublisher(msg_type, topic, qos)
        self.publishers.append(publisher)
        return publisher

    def create_subscription(self, msg_type, topic, callback, qos) -> _FakeSubscription:
        subscription = _FakeSubscription(msg_type, topic, callback, qos)
        self.subscriptions.append(subscription)
        return subscription

    def create_timer(self, period_s, callback) -> _FakeTimer:
        timer = _FakeTimer(period_s, callback)
        self.timers.append(timer)
        return timer

    def get_clock(self) -> _FakeClock:
        return _FakeClock()

    def destroy_node(self) -> None:
        self.destroyed = True

    def subscription(self, topic: str) -> _FakeSubscription:
        return next(sub for sub in self.subscriptions if sub.topic == topic)

    def publisher(self, topic: str) -> _FakePublisher:
        return next(pub for pub in self.publishers if pub.topic == topic)


class _ExternalShutdown(Exception):
    pass


class _FakeExecutor:
    def __init__(self) -> None:
        self.spins = 0
        self.node: _FakeNode | None = None
        self.script = None

    def add_node(self, node) -> None:
        self.node = node

    def remove_node(self, node) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def spin_once(self, timeout_sec: float = 0.0) -> None:
        self.spins += 1
        if self.script is not None:
            self.script(self)


def _install_fake_ros_modules(monkeypatch) -> dict[str, ModuleType]:
    """Install the minimal rclpy/sensor_msgs/std_msgs surface the entry imports in _serve."""
    rclpy = ModuleType("rclpy")
    rclpy.init = lambda *args, **kwargs: None
    rclpy.shutdown = lambda: None
    rclpy.ok = lambda: True

    executors = ModuleType("rclpy.executors")
    executors.ExternalShutdownException = _ExternalShutdown
    executors.SingleThreadedExecutor = _FakeExecutor
    nodes = ModuleType("rclpy.node")
    nodes.Node = _FakeNode
    signals = ModuleType("rclpy.signals")
    signals.SignalHandlerOptions = SimpleNamespace(NO="NO")
    qos = ModuleType("rclpy.qos")
    qos.DurabilityPolicy = SimpleNamespace(TRANSIENT_LOCAL="transient_local", VOLATILE="volatile")
    qos.HistoryPolicy = SimpleNamespace(KEEP_LAST="keep_last")
    qos.ReliabilityPolicy = SimpleNamespace(RELIABLE="reliable")
    qos.QoSProfile = lambda **kwargs: kwargs
    joint_state_msgs = ModuleType("sensor_msgs.msg")
    joint_state_msgs.JointState = _FakeJointState
    bool_msgs = ModuleType("std_msgs.msg")
    bool_msgs.Bool = _FakeBool

    modules = {
        "rclpy": rclpy,
        "rclpy.executors": executors,
        "rclpy.node": nodes,
        "rclpy.signals": signals,
        "rclpy.qos": qos,
        "sensor_msgs": ModuleType("sensor_msgs"),
        "sensor_msgs.msg": joint_state_msgs,
        "std_msgs": ModuleType("std_msgs"),
        "std_msgs.msg": bool_msgs,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    rclpy.executors = executors
    rclpy.node = nodes
    rclpy.signals = signals
    rclpy.qos = qos
    return modules


def _mock_round_script(executor: _FakeExecutor) -> None:
    """Warmup sample -> one reset edge -> external shutdown while the round runs."""
    spins = executor.spins
    node = executor.node
    assert node is not None
    if spins == 1:
        message = _FakeJointState()
        message.name = [
            "shoulder_pan",
            "shoulder_lift",
            "elbow_flex",
            "wrist_flex",
            "wrist_roll",
            "gripper",
        ]
        message.position = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        node.subscription("/arm/joint_states").callback(message)
    elif spins == 2:
        node.subscription("/env/reset_status").callback(_FakeBool(data=False))
    elif spins == 3:
        node.subscription("/env/reset_status").callback(_FakeBool(data=True))
    elif spins >= 6:
        # External rclpy shutdown while the policy round runs: exit cleanly (0).
        raise _ExternalShutdown()


def _run_entry_with_fake_ros(monkeypatch, config: Path, *args: str) -> int:
    """Run ``entry.main`` in-process with fake ROS modules and a scripted executor."""
    _FakeNode.instances.clear()
    modules = _install_fake_ros_modules(monkeypatch)
    for key in ("INFERENCE_NAMESPACE", "ROS_DOMAIN_ID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in INSTANCE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(entry, "FAILURE_PUBLISH_DRAIN_S", 0.0)
    executor = _FakeExecutor()
    executor.script = _mock_round_script
    monkeypatch.setattr(modules["rclpy.executors"], "SingleThreadedExecutor", lambda: executor)
    return entry.main(["--config", str(config), *args])


def test_entry_main_runs_one_mock_round_end_to_end_without_ros(monkeypatch, capsys):
    """Fake rclpy + real backend/runner: one reset edge runs the real mock child, its report
    reaches the round_finished event, and no ROS/GPU/hardware is touched."""
    result = _run_entry_with_fake_ros(monkeypatch, MOCK_CONFIG, "--dry-run")
    captured = capsys.readouterr()
    assert result == 0, captured.err
    events = json_events(captured.out)
    finished = [event for event in events if event["event"] == "round_finished"]
    assert len(finished) == 1
    assert finished[0]["status"] == "ok"
    assert finished[0]["backend"] == "mock"
    assert finished[0]["steps"] == 30
    assert finished[0]["published"] == 30
    assert finished[0]["checkpoint"] is None
    assert finished[0]["returncode"] == 0
    nodes = _FakeNode.instances
    assert len(nodes) == 1
    stopped_values = [
        message.data for message in nodes[0].publisher("/inference/stopped").published
    ]
    assert stopped_values == [True, True, False, True]
    assert nodes[0].destroyed is True


def test_entry_dry_run_forces_mock_even_for_the_live_finetune_config(monkeypatch, capsys):
    """--dry-run on the live config must run the offline mock round (never finetune)."""
    result = _run_entry_with_fake_ros(monkeypatch, LIVE_CONFIG, "--dry-run")
    captured = capsys.readouterr()
    assert result == 0, captured.err
    events = json_events(captured.out)
    starting = next(event for event in events if event["event"] == "instance_starting")
    assert starting["backend"] == "finetune"  # config declared, but --dry-run forces mock
    assert starting["dry_run"] is True
    finished = [event for event in events if event["event"] == "round_finished"]
    assert len(finished) == 1
    assert finished[0]["backend"] == "mock"
    assert finished[0]["published"] == 30


def test_entry_refuses_a_finetune_round_without_the_motion_gates(monkeypatch, capsys):
    """Live config without --enable-motion/--confirm-real: no child, exit 1, clear gate."""
    result = _run_entry_with_fake_ros(monkeypatch, LIVE_CONFIG)
    captured = capsys.readouterr()
    assert result == 1, captured.err
    events = json_events(captured.out)
    assert not [event for event in events if event["event"] == "round_finished"]
    failed = [event for event in events if event["event"] == "instance_failed"]
    assert failed and failed[0]["stage"] == "backend"
    assert "--enable-motion" in str(failed[0]["error"])
    assert "--confirm-real" in str(failed[0]["error"])
