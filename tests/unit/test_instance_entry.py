"""Purpose: unit-test the instance entry CLI and the rclpy wiring with a fake rclpy.

No real ROS: the happy path is driven through a fake node/executor/timer harness, and the
failure paths force ``import rclpy`` to fail (exit code 2 + JSON ``instance_failed``).

Usage: env -u PYTHONPATH .venv/bin/python -m pytest tests/unit/test_instance_entry.py -q
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from policy_base.instance import entry
from policy_base.instance.config import (
    DEFAULT_JOINT_NAMES,
    InstanceConfig,
    InstanceSettings,
    Ros2Settings,
)
from policy_base.instance.keepalive import Keepalive
from policy_base.instance.protocol import (
    CHILD_INTERRUPTED,
    CHILD_OK,
    CHILD_TIMEOUT,
    InstanceEnv,
)
from policy_base.instance.runner import ChildOutcome
from policy_base.policy.config import PolicyConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
MOCK_CONFIG = REPO_ROOT / "configs/pick_place/mock_ros2_env.yaml"
LIVE_CONFIG = REPO_ROOT / "configs/pick_place/live_ros2_env.yaml"

VALID_ENV = {
    "INFERENCE_NAMESPACE": "",
    "DOMAIN_ID": "229",
    "STATION_TYPE": "pick_place",
    "CLIENT_ID": "client-1",
    "LEASE_ID": "7",
    "INFERENCE_ID": "3",
}
JOINTS = list(DEFAULT_JOINT_NAMES)
POSITIONS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
OK_REPORT = {
    "schema": "so101.policy_base.round_report.v1",
    "status": "ok",
    "backend": "mock",
    "checkpoint": None,
    "steps": 30,
    "published": 30,
    "duration_s": 0.05,
}


def apply_env(monkeypatch, overrides: dict[str, str | None] | None = None) -> None:
    """Isolate the instance env: only VALID_ENV (+ overrides, None = unset)."""
    for key in (
        "INFERENCE_NAMESPACE",
        "DOMAIN_ID",
        "STATION_TYPE",
        "CLIENT_ID",
        "LEASE_ID",
        "INFERENCE_ID",
        "ROS_DOMAIN_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    values: dict[str, str | None] = dict(VALID_ENV)
    values.update(overrides or {})
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def json_lines(capsys) -> list[dict[str, Any]]:
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines, "entry printed no structured log lines"
    return [json.loads(line) for line in lines]


def force_ros_unavailable(monkeypatch) -> None:
    """Make ``import rclpy`` fail so the entry stops before touching any ROS graph."""
    monkeypatch.setitem(sys.modules, "rclpy", None)


def make_config(
    *,
    namespace_env: str = "INFERENCE_NAMESPACE",
    backend: str = "mock",
    allow_platform_execution: bool = False,
    checkpoint: str | None = None,
    warmup_timeout_s: float = 30.0,
    round_timeout_s: float = 600.0,
    hold_hz: float = 4.0,
    stopped_hz: float = 2.0,
) -> InstanceConfig:
    policy = PolicyConfig(
        backend=backend,
        checkpoint=checkpoint,
        allow_platform_execution=allow_platform_execution,
    )
    return InstanceConfig(
        instance=InstanceSettings(
            namespace_env=namespace_env,
            warmup_timeout_s=warmup_timeout_s,
            round_timeout_s=round_timeout_s,
            hold_hz=hold_hz,
            stopped_hz=stopped_hz,
        ),
        ros2=Ros2Settings(),
        policy=policy,
    )


def make_env(namespace: str = "") -> InstanceEnv:
    return InstanceEnv(
        namespace=namespace,
        domain_id=229,
        station_type="pick_place",
        client_id="client-1",
        lease_id="7",
        inference_id=3,
    )


# --- CLI ------------------------------------------------------------------
def test_config_argument_is_required_with_exit_code_2(capsys):
    with pytest.raises(SystemExit) as excinfo:
        entry.parse_args([])
    assert excinfo.value.code == 2
    assert "--config" in capsys.readouterr().err


def test_help_works_without_ros(capsys):
    with pytest.raises(SystemExit) as excinfo:
        entry.parse_args(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "policy_base ACT instance entry" in out
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
        assert option in out


def test_defaults_defer_timeouts_to_the_configuration():
    args = entry.parse_args(["--config", str(MOCK_CONFIG)])
    assert args.dry_run is False
    assert args.enable_motion is False
    assert args.confirm_real is False
    assert args.warmup_timeout is None
    assert args.round_timeout is None
    assert args.hold_hz is None
    assert args.stop_grace == 5.0


@pytest.mark.parametrize("argv", [["--hold-hz", "1.0"], ["--hold-hz", "5.5"]])
def test_hold_hz_outside_contract_band_is_rejected(argv):
    with pytest.raises(SystemExit) as excinfo:
        entry.parse_args(["--config", str(MOCK_CONFIG), *argv])
    assert excinfo.value.code == 2


def test_invalid_timeouts_and_grace_are_rejected():
    for argv in (
        ["--warmup-timeout", "0"],
        ["--round-timeout", "-1"],
        ["--stop-grace", "-0.1"],
    ):
        with pytest.raises(SystemExit) as excinfo:
            entry.parse_args(["--config", str(MOCK_CONFIG), *argv])
        assert excinfo.value.code == 2


def test_runtime_options_use_config_defaults():
    args = entry.parse_args(["--config", str(MOCK_CONFIG)])
    options = entry.resolve_runtime_options(
        args,
        InstanceSettings(
            warmup_timeout_s=12.0,
            round_timeout_s=34.0,
            hold_hz=4.5,
            stopped_hz=2.0,
        ),
    )
    assert options.warmup_timeout_s == 12.0
    assert options.round_timeout_s == 34.0
    assert options.hold_hz == 4.5
    assert options.stop_grace_s == 5.0


def test_runtime_options_cli_overrides_win():
    args = entry.parse_args(
        [
            "--config",
            str(MOCK_CONFIG),
            "--warmup-timeout",
            "3",
            "--round-timeout",
            "7",
            "--hold-hz",
            "2.5",
            "--stop-grace",
            "0.5",
        ]
    )
    options = entry.resolve_runtime_options(args, InstanceSettings())
    assert options.warmup_timeout_s == 3.0
    assert options.round_timeout_s == 7.0
    assert options.hold_hz == 2.5
    assert options.stop_grace_s == 0.5


# --- config / environment failure paths -----------------------------------
def test_config_load_error_exits_2_with_config_stage(monkeypatch, capsys):
    apply_env(monkeypatch)

    def failing_load(path: Path) -> InstanceConfig:
        raise ValueError("bad instance config")

    monkeypatch.setattr(entry, "load_instance_config", failing_load)
    assert entry.main(["--config", str(MOCK_CONFIG)]) == 2
    lines = json_lines(capsys)
    assert lines[0]["event"] == "instance_failed"
    assert lines[0]["stage"] == "config"
    assert "bad instance config" in lines[0]["error"]


def test_missing_config_file_exits_2_with_config_stage(monkeypatch, capsys):
    apply_env(monkeypatch)
    assert entry.main(["--config", str(REPO_ROOT / "does-not-exist.yaml")]) == 2
    lines = json_lines(capsys)
    assert lines[0]["event"] == "instance_failed"
    assert lines[0]["stage"] == "config"


def test_missing_domain_id_exits_2_with_environment_stage(monkeypatch, capsys):
    apply_env(monkeypatch, {"DOMAIN_ID": None})
    monkeypatch.setattr(entry, "load_instance_config", lambda path: make_config())
    assert entry.main(["--config", str(MOCK_CONFIG)]) == 2
    lines = json_lines(capsys)
    assert lines[0]["event"] == "instance_failed"
    assert lines[0]["stage"] == "environment"
    assert "DOMAIN_ID" in lines[0]["error"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"STATION_TYPE": None},
        {"STATION_TYPE": "drawer"},
        {"ROS_DOMAIN_ID": "193"},
        {"INFERENCE_NAMESPACE": "station1"},
    ],
)
def test_invalid_environment_exits_2(monkeypatch, capsys, overrides):
    apply_env(monkeypatch, overrides)
    monkeypatch.setattr(entry, "load_instance_config", lambda path: make_config())
    assert entry.main(["--config", str(MOCK_CONFIG)]) == 2
    assert json_lines(capsys)[0]["stage"] == "environment"


def test_domain_id_is_exported_as_ros_domain_id(monkeypatch):
    import os

    apply_env(monkeypatch, {"DOMAIN_ID": "229", "ROS_DOMAIN_ID": None})
    captured: dict[str, Any] = {}

    def fake_serve(args, env, config, keepalive, options):
        captured.update(env=env, config=config, keepalive=keepalive, options=options)
        return 0

    monkeypatch.setattr(entry, "load_instance_config", lambda path: make_config())
    monkeypatch.setattr(entry, "_serve", fake_serve)
    assert entry.main(["--config", str(MOCK_CONFIG), "--dry-run"]) == 0
    assert os.environ["ROS_DOMAIN_ID"] == "229"
    assert captured["env"].domain_id == 229


def test_main_reads_namespace_from_the_configured_variable(monkeypatch):
    apply_env(monkeypatch, {"INFERENCE_NAMESPACE": None})
    monkeypatch.setenv("POLICY_NS", "/station9")
    captured: dict[str, Any] = {}

    def fake_serve(args, env, config, keepalive, options):
        captured["env"] = env
        return 0

    monkeypatch.setattr(
        entry,
        "load_instance_config",
        lambda path: make_config(namespace_env="POLICY_NS"),
    )
    monkeypatch.setattr(entry, "_serve", fake_serve)
    assert entry.main(["--config", str(MOCK_CONFIG)]) == 0
    assert captured["env"].namespace == "/station9"
    assert captured["env"].stopped_topic == "/station9/inference/stopped"


def test_startup_log_fields_and_warnings(monkeypatch, capsys):
    apply_env(
        monkeypatch,
        {"INFERENCE_NAMESPACE": "/station1", "CLIENT_ID": "", "LEASE_ID": "", "INFERENCE_ID": "42"},
    )
    monkeypatch.setattr(
        entry,
        "load_instance_config",
        lambda path: make_config(backend="finetune", allow_platform_execution=True, checkpoint="m"),
    )
    monkeypatch.setattr(entry, "_serve", lambda *args: 0)
    assert entry.main(["--config", str(MOCK_CONFIG), "--enable-motion", "--confirm-real"]) == 0
    lines = json_lines(capsys)
    starting = next(line for line in lines if line["event"] == "instance_starting")
    assert starting["namespace"] == "/station1"
    assert starting["domain_id"] == 229
    assert starting["station_type"] == "pick_place"
    assert starting["inference_id"] == 42
    assert starting["dry_run"] is False
    assert starting["enable_motion"] is True
    assert starting["confirm_real"] is True
    assert starting["backend"] == "finetune"
    warnings = [line["warning"] for line in lines if line["event"] == "instance_warning"]
    assert any("CLIENT_ID" in warning for warning in warnings)
    assert any("LEASE_ID" in warning for warning in warnings)


def test_effective_options_are_passed_to_the_serve_loop(monkeypatch):
    apply_env(monkeypatch)
    captured: dict[str, Any] = {}

    def fake_serve(args, env, config, keepalive, options):
        captured.update(config=config, keepalive=keepalive, options=options, args=args)
        return 0

    monkeypatch.setattr(
        entry,
        "load_instance_config",
        lambda path: make_config(warmup_timeout_s=12.0, round_timeout_s=34.0, hold_hz=4.0),
    )
    monkeypatch.setattr(entry, "_serve", fake_serve)
    assert entry.main(["--config", str(MOCK_CONFIG), "--warmup-timeout", "3"]) == 0
    options = captured["options"]
    assert options.warmup_timeout_s == 3.0
    assert options.round_timeout_s == 34.0
    assert options.hold_hz == 4.0
    keepalive = captured["keepalive"]
    assert isinstance(keepalive, Keepalive)
    assert keepalive.hz == 4.0
    assert keepalive.joint_names == DEFAULT_JOINT_NAMES


def test_missing_rclpy_exits_2_with_ros2_init_stage(monkeypatch, capsys):
    apply_env(monkeypatch)
    force_ros_unavailable(monkeypatch)
    assert entry.main(["--config", str(MOCK_CONFIG), "--dry-run"]) == 2
    lines = json_lines(capsys)
    assert lines[-1]["event"] == "instance_failed"
    assert lines[-1]["stage"] == "ros2_init"


def test_real_mock_config_loads_and_fails_cleanly_without_ros(monkeypatch, capsys):
    """The declared mock config must be loadable by the real loaders (no ROS needed)."""
    apply_env(monkeypatch)
    force_ros_unavailable(monkeypatch)
    assert entry.main(["--config", str(MOCK_CONFIG), "--dry-run"]) == 2
    lines = json_lines(capsys)
    assert lines[0]["event"] == "instance_starting"
    assert lines[0]["backend"] == "mock"
    assert lines[0]["dry_run"] is True
    assert lines[-1]["event"] == "instance_failed"
    assert lines[-1]["stage"] == "ros2_init"


def test_real_live_config_loads_and_fails_cleanly_without_ros(monkeypatch, capsys):
    apply_env(monkeypatch)
    force_ros_unavailable(monkeypatch)
    assert entry.main(["--config", str(LIVE_CONFIG)]) == 2
    lines = json_lines(capsys)
    assert lines[0]["event"] == "instance_starting"
    assert lines[0]["backend"] == "finetune"
    assert lines[0]["dry_run"] is False
    assert lines[-1]["event"] == "instance_failed"
    assert lines[-1]["stage"] == "ros2_init"


# --- fake rclpy harness ----------------------------------------------------
class FakeBool:
    def __init__(self, *, data: bool = False) -> None:
        self.data = data


class FakeJointState:
    def __init__(self) -> None:
        self.header = SimpleNamespace(stamp=None)
        self.name: list[str] = []
        self.position: list[float] = []


class FakePublisher:
    def __init__(self, msg_type: Any, topic: str, qos: Any) -> None:
        self.msg_type = msg_type
        self.topic = topic
        self.qos = qos
        self.published: list[Any] = []

    def publish(self, message: Any) -> None:
        self.published.append(message)


class FakeSubscription:
    def __init__(self, msg_type: Any, topic: str, callback: Any, qos: Any) -> None:
        self.msg_type = msg_type
        self.topic = topic
        self.callback = callback
        self.qos = qos


class FakeTimer:
    def __init__(self, period_s: float, callback: Any) -> None:
        self.period_s = period_s
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeClock:
    def now(self) -> SimpleNamespace:
        return SimpleNamespace(to_msg=lambda: "stamp")


class FakeNode:
    def __init__(self, name: str = "fake_node") -> None:
        self.name = name
        self.publishers: list[FakePublisher] = []
        self.subscriptions: list[FakeSubscription] = []
        self.timers: list[FakeTimer] = []
        self.destroyed = False

    def create_publisher(self, msg_type: Any, topic: str, qos: Any) -> FakePublisher:
        publisher = FakePublisher(msg_type, topic, qos)
        self.publishers.append(publisher)
        return publisher

    def create_subscription(
        self, msg_type: Any, topic: str, callback: Any, qos: Any
    ) -> FakeSubscription:
        subscription = FakeSubscription(msg_type, topic, callback, qos)
        self.subscriptions.append(subscription)
        return subscription

    def create_timer(self, period_s: float, callback: Any) -> FakeTimer:
        timer = FakeTimer(period_s, callback)
        self.timers.append(timer)
        return timer

    def get_clock(self) -> FakeClock:
        return FakeClock()

    def destroy_node(self) -> None:
        self.destroyed = True

    def subscription(self, topic: str) -> FakeSubscription:
        return next(sub for sub in self.subscriptions if sub.topic == topic)

    def publisher(self, topic: str) -> FakePublisher:
        return next(pub for pub in self.publishers if pub.topic == topic)

    def timer(self, period_s: float) -> FakeTimer:
        return next(timer for timer in self.timers if timer.period_s == pytest.approx(period_s))


class FakeExecutor:
    def __init__(self, script: Any = None) -> None:
        self.spins = 0
        self.node: FakeNode | None = None
        self.script = script
        self.removed = False
        self.shutdown_called = False

    def add_node(self, node: FakeNode) -> None:
        self.node = node

    def remove_node(self, node: FakeNode) -> None:
        self.removed = True

    def shutdown(self) -> None:
        self.shutdown_called = True

    def spin_once(self, timeout_sec: float = 0.0) -> None:
        self.spins += 1
        if self.script is not None:
            self.script(self)


class FakeRclpy:
    def __init__(self) -> None:
        self.ok_value = True
        self.shutdown_called = False

    def ok(self) -> bool:
        return self.ok_value

    def shutdown(self) -> None:
        self.shutdown_called = True


class FakeRunner:
    """Minimal RoundRunner double capturing the command and observations."""

    def __init__(self, outcome: ChildOutcome, observe: Any = None) -> None:
        self.outcome = outcome
        self.observe = observe
        self.commands: list[Any] = []
        self.interrupted = False

    def interrupt(self) -> None:
        self.interrupted = True

    def run(self, command: Any, *, on_tick: Any = None) -> ChildOutcome:
        self.commands.append(command)
        if self.observe is not None:
            self.observe()
        if on_tick is not None:
            on_tick()
            on_tick()
        return self.outcome


def install_fake_rclpy_qos(monkeypatch) -> types.ModuleType:
    module = types.ModuleType("rclpy.qos")
    module.DurabilityPolicy = SimpleNamespace(
        TRANSIENT_LOCAL="transient_local", VOLATILE="volatile"
    )
    module.HistoryPolicy = SimpleNamespace(KEEP_LAST="keep_last")
    module.ReliabilityPolicy = SimpleNamespace(RELIABLE="reliable", BEST_EFFORT="best_effort")

    class FakeQoSProfile:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    module.QoSProfile = FakeQoSProfile
    monkeypatch.setitem(sys.modules, "rclpy.qos", module)
    return module


def build_app(
    monkeypatch,
    tmp_path: Path,
    *,
    runner: FakeRunner,
    script: Any = None,
    config: InstanceConfig | None = None,
    warmup_timeout_s: float = 0.05,
    enable_motion: bool = False,
    confirm_real: bool = False,
    dry_run: bool = True,
) -> SimpleNamespace:
    install_fake_rclpy_qos(monkeypatch)
    config = config or make_config()
    env = make_env()
    keepalive = Keepalive(config.ros2.joint_names, hz=config.instance.hold_hz)
    node = FakeNode()
    executor = FakeExecutor(script)
    executor.add_node(node)
    shutdown = entry._Shutdown()
    rclpy_module = FakeRclpy()
    app = entry._InstanceApp(
        node=node,
        executor=executor,
        config=config,
        env=env,
        keepalive=keepalive,
        runner=runner,
        report_dir=tmp_path,
        shutdown=shutdown,
        warmup_timeout_s=warmup_timeout_s,
        enable_motion=enable_motion,
        confirm_real=confirm_real,
        dry_run=dry_run,
        rclpy_module=rclpy_module,
        external_shutdown=RuntimeError,
        joint_state_type=FakeJointState,
        bool_type=FakeBool,
    )
    return SimpleNamespace(
        app=app,
        node=node,
        executor=executor,
        keepalive=keepalive,
        shutdown=shutdown,
        rclpy=rclpy_module,
        config=config,
        env=env,
    )


def joint_state_message() -> FakeJointState:
    message = FakeJointState()
    message.name = list(JOINTS)
    message.position = list(POSITIONS)
    return message


def make_script(
    state: dict[str, Any],
    *,
    feed_joints: bool = True,
    round_edges: bool = True,
    shutdown_after_spins: int | None = None,
) -> Any:
    """Drive one warmup sample, one reset edge and an optional shutdown via spin count."""

    def script(executor: FakeExecutor) -> None:
        spins = executor.spins
        node = executor.node
        assert node is not None
        if feed_joints and spins == 1:
            node.subscription("/arm/joint_states").callback(joint_state_message())
        elif round_edges and spins == 2:
            node.subscription("/env/reset_status").callback(FakeBool(data=False))
        elif round_edges and spins == 3:
            node.subscription("/env/reset_status").callback(FakeBool(data=True))
        if shutdown_after_spins is not None and spins >= shutdown_after_spins:
            state["shutdown"].requested = True

    return script


def events_of(lines: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [line for line in lines if line["event"] == name]


def test_successful_round_cycle(monkeypatch, tmp_path, capsys):
    state: dict[str, Any] = {}
    observations: dict[str, Any] = {}

    def observe() -> None:
        app = state["app"]
        observations["keepalive_running"] = app.keepalive.running
        observations["stopped"] = app.stopped_pub.published[-1].data
        app._on_hold_timer()  # must not publish: keepalive is paused during the round

    runner = FakeRunner(
        ChildOutcome(status=CHILD_OK, returncode=0, stage="round", report=dict(OK_REPORT)),
        observe=observe,
    )
    bundle = build_app(
        monkeypatch, tmp_path, runner=runner, script=make_script(state, shutdown_after_spins=6)
    )
    state["app"] = bundle.app
    state["shutdown"] = bundle.shutdown
    assert bundle.app.run() == 0

    # stopped: initial latched True, ready True, round False, round finished True.
    stopped_values = [
        message.data for message in bundle.node.publisher("/inference/stopped").published
    ]
    assert stopped_values == [True, True, False, True]

    # Hold heartbeat is strictly paused while the policy child owns the arm traffic.
    assert observations["keepalive_running"] is False
    assert observations["stopped"] is False
    assert bundle.node.publisher("/arm/joint_command").published == []

    # The frozen RoundCommand was requested with a per-round report path.
    assert len(runner.commands) == 1
    command = runner.commands[0]
    assert command.description == "mock"
    assert command.report_path == tmp_path / "round_0001.json"
    assert command.argv[1:3] == ("-m", "policy_base.policy.mock")
    assert str(command.report_path) in command.argv

    lines = json_lines(capsys)
    topics = events_of(lines, "instance_topics")[0]
    assert topics["stopped"] == "/inference/stopped"
    assert topics["reset_status"] == "/env/reset_status"
    assert topics["joint_states"] == "/arm/joint_states"
    assert topics["joint_command"] == "/arm/joint_command"
    ready = events_of(lines, "instance_ready")[0]
    assert ready["hold_hz"] == 4.0
    started = events_of(lines, "round_started")[0]
    assert started["round"] == 1
    assert started["dry_run"] is True
    finished = events_of(lines, "round_finished")[0]
    assert finished["round"] == 1
    assert finished["status"] == "ok"
    assert finished["returncode"] == 0
    assert finished["backend"] == "mock"
    assert finished["steps"] == 30
    assert finished["published"] == 30
    assert finished["checkpoint"] is None
    assert finished["rounds_completed"] == 1
    stopping = events_of(lines, "instance_stopping")[0]
    assert stopping["rounds_completed"] == 1
    assert stopping["rounds_failed"] == 0

    # Timers exist at the configured rates and are cancelled on teardown.
    assert bundle.node.timer(0.5).period_s == pytest.approx(1 / 2.0)
    assert bundle.node.timer(0.25).period_s == pytest.approx(1 / 4.0)
    assert all(timer.cancelled for timer in bundle.node.timers)
    assert bundle.keepalive.running is False


def test_report_missing_still_completes_the_round(monkeypatch, tmp_path, capsys):
    state: dict[str, Any] = {}
    runner = FakeRunner(ChildOutcome(status=CHILD_OK, returncode=0, stage="round", report=None))
    bundle = build_app(
        monkeypatch, tmp_path, runner=runner, script=make_script(state, shutdown_after_spins=6)
    )
    state["shutdown"] = bundle.shutdown
    assert bundle.app.run() == 0
    finished = events_of(json_lines(capsys), "round_finished")[0]
    assert finished["status"] == "ok"
    assert finished["published"] == 0
    assert finished["backend"] is None


def test_dry_run_forces_the_mock_backend_even_for_a_finetune_config(monkeypatch, tmp_path, capsys):
    """--dry-run must short-circuit the finetune safety gates (offline testing only)."""
    state: dict[str, Any] = {}
    runner = FakeRunner(
        ChildOutcome(status=CHILD_OK, returncode=0, stage="round", report=dict(OK_REPORT))
    )
    config = make_config(backend="finetune", allow_platform_execution=False, checkpoint="models/x")
    bundle = build_app(
        monkeypatch,
        tmp_path,
        runner=runner,
        script=make_script(state, shutdown_after_spins=6),
        config=config,
        dry_run=True,
    )
    state["shutdown"] = bundle.shutdown
    assert bundle.app.run() == 0
    assert runner.commands[0].description == "mock"


def test_live_round_requires_all_safety_gates(monkeypatch, tmp_path, capsys):
    """A finetune round without the CLI/config gates fails as a round (exit 1)."""
    monkeypatch.setattr(entry, "FAILURE_PUBLISH_DRAIN_S", 0.0)
    state: dict[str, Any] = {}
    runner = FakeRunner(
        ChildOutcome(status=CHILD_OK, returncode=0, stage="round", report=dict(OK_REPORT))
    )
    config = make_config(backend="finetune", allow_platform_execution=False, checkpoint="models/x")
    bundle = build_app(
        monkeypatch,
        tmp_path,
        runner=runner,
        script=make_script(state, round_edges=True),
        config=config,
        dry_run=False,
        enable_motion=True,
        confirm_real=True,
    )
    state["shutdown"] = bundle.shutdown
    assert bundle.app.run() == 1
    assert runner.commands == []  # no child was ever built/spawned
    failed = events_of(json_lines(capsys), "instance_failed")[0]
    assert failed["stage"] == "backend"
    assert "allow_platform_execution" in failed["error"]
    assert failed["round"] == 1


def test_runner_exception_fails_the_instance_without_leaking(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(entry, "FAILURE_PUBLISH_DRAIN_S", 0.0)
    state: dict[str, Any] = {}

    class ExplodingRunner(FakeRunner):
        def run(self, command: Any, *, on_tick: Any = None) -> ChildOutcome:
            raise RuntimeError("runner bug")

    runner = ExplodingRunner(ChildOutcome(status=CHILD_OK, returncode=0, stage="round"))
    bundle = build_app(
        monkeypatch, tmp_path, runner=runner, script=make_script(state, round_edges=True)
    )
    state["shutdown"] = bundle.shutdown
    assert bundle.app.run() == 1
    failed = events_of(json_lines(capsys), "instance_failed")[0]
    assert failed["stage"] == "round"
    assert "runner bug" in failed["error"]


def test_timeout_round_exits_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(entry, "FAILURE_PUBLISH_DRAIN_S", 0.0)
    state: dict[str, Any] = {}
    runner = FakeRunner(
        ChildOutcome(status=CHILD_TIMEOUT, returncode=-9, stage="timeout", error="round exceeded")
    )
    bundle = build_app(
        monkeypatch, tmp_path, runner=runner, script=make_script(state, round_edges=True)
    )
    state["shutdown"] = bundle.shutdown
    assert bundle.app.run() == 1
    failed = events_of(json_lines(capsys), "instance_failed")[0]
    assert failed["stage"] == "timeout"
    assert failed["status"] == "timeout"
    assert failed["returncode"] == -9


def test_interrupted_round_exits_0(monkeypatch, tmp_path, capsys):
    state: dict[str, Any] = {}
    runner = FakeRunner(
        ChildOutcome(status=CHILD_INTERRUPTED, returncode=-15, stage="round", error="SIGTERM")
    )
    bundle = build_app(
        monkeypatch, tmp_path, runner=runner, script=make_script(state, round_edges=True)
    )
    state["shutdown"] = bundle.shutdown
    assert bundle.app.run() == 0
    lines = json_lines(capsys)
    interrupted = events_of(lines, "round_interrupted")[0]
    assert interrupted["returncode"] == -15
    assert interrupted["status"] == "interrupted"


def test_warmup_timeout_exits_2(monkeypatch, tmp_path, capsys):
    runner = FakeRunner(ChildOutcome(status=CHILD_OK, returncode=0, stage="round"))
    bundle = build_app(
        monkeypatch,
        tmp_path,
        runner=runner,
        script=None,
        warmup_timeout_s=0.02,
    )
    assert bundle.app.run() == 2
    failed = events_of(json_lines(capsys), "instance_failed")[0]
    assert failed["stage"] == "warmup"
    assert "/arm/joint_states" in failed["error"]
    assert bundle.keepalive.running is False
    assert all(timer.cancelled for timer in bundle.node.timers)


def test_shutdown_during_warmup_exits_0(monkeypatch, tmp_path, capsys):
    state: dict[str, Any] = {}

    def script(executor: FakeExecutor) -> None:
        state["shutdown"].requested = True

    runner = FakeRunner(ChildOutcome(status=CHILD_OK, returncode=0, stage="round"))
    bundle = build_app(monkeypatch, tmp_path, runner=runner, script=script)
    state["shutdown"] = bundle.shutdown
    assert bundle.app.run() == 0
    lines = json_lines(capsys)
    stopping = events_of(lines, "instance_stopping")[0]
    assert stopping["stage"] == "warmup"


def test_hold_heartbeat_publishes_measured_positions_when_idle(monkeypatch, tmp_path):
    runner = FakeRunner(ChildOutcome(status=CHILD_OK, returncode=0, stage="round"))
    bundle = build_app(monkeypatch, tmp_path, runner=runner, config=make_config(hold_hz=2.0))
    # Feed a measured sample and start the idle heartbeat (as the app does after warmup).
    bundle.node.subscription("/arm/joint_states").callback(joint_state_message())
    bundle.keepalive.start()
    bundle.app._on_hold_timer()
    published = bundle.node.publisher("/arm/joint_command").published
    assert len(published) == 1
    assert published[0].name == JOINTS
    assert published[0].position == POSITIONS
    assert published[0].header.stamp == "stamp"
    bundle.app.close()
    bundle.app._on_hold_timer()  # stopped: no further publishes
    assert len(bundle.node.publisher("/arm/joint_command").published) == 1


def test_reset_edges_are_strict_and_dropped_while_running(monkeypatch, tmp_path):
    """Latched True never re-triggers; a reset during the round is dropped, not queued."""
    state: dict[str, Any] = {}

    def script(executor: FakeExecutor) -> None:
        spins = executor.spins
        node = executor.node
        assert node is not None
        if spins == 1:
            node.subscription("/arm/joint_states").callback(joint_state_message())
        elif spins == 2:
            # Latched True without a fresh False level: not an edge, no round starts.
            node.subscription("/env/reset_status").callback(FakeBool(data=True))
        elif spins == 3:
            node.subscription("/env/reset_status").callback(FakeBool(data=False))
        elif spins == 4:
            node.subscription("/env/reset_status").callback(FakeBool(data=True))
        elif spins == 5:
            # Reset during the policy round: must be dropped, never queued for round 2.
            node.subscription("/env/reset_status").callback(FakeBool(data=True))
        elif spins >= 6:
            state["app"].shutdown.requested = True

    runner = FakeRunner(
        ChildOutcome(status=CHILD_OK, returncode=0, stage="round", report=dict(OK_REPORT))
    )
    bundle = build_app(monkeypatch, tmp_path, runner=runner, script=script)
    state["app"] = bundle.app
    assert bundle.app.run() == 0
    assert bundle.app.gate.rounds_started == 1
    assert bundle.app.gate.rounds_completed == 1
    assert bundle.app.gate.discarded_resets == 1


def test_close_is_idempotent(monkeypatch, tmp_path):
    runner = FakeRunner(ChildOutcome(status=CHILD_OK, returncode=0, stage="round"))
    bundle = build_app(monkeypatch, tmp_path, runner=runner)
    bundle.app.close()
    bundle.app.close()
    assert all(timer.cancelled for timer in bundle.node.timers)


def test_serve_returns_config_error_when_rclpy_is_missing(monkeypatch, capsys):
    apply_env(monkeypatch)
    force_ros_unavailable(monkeypatch)
    config = make_config()
    keepalive = Keepalive(config.ros2.joint_names)
    options = entry.RuntimeOptions(
        warmup_timeout_s=30.0,
        round_timeout_s=600.0,
        hold_hz=4.0,
        stop_grace_s=5.0,
    )
    args = entry.parse_args(["--config", str(MOCK_CONFIG)])
    assert entry._serve(args, make_env(), config, keepalive, options) == 2
    failed = json_lines(capsys)[-1]
    assert failed["event"] == "instance_failed"
    assert failed["stage"] == "ros2_init"
