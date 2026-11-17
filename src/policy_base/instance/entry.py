"""IM instance entrypoint for the SO-101 policy_base ACT program.

The inference manager launches this module (``python3 -m policy_base.instance.entry
--config <yaml> [--dry-run]``) instead of the station CLI. It implements the frozen
instance protocol (same wire contract as rule_base instance mode):

* read ``INFERENCE_NAMESPACE`` / ``DOMAIN_ID`` / ``STATION_TYPE`` / ``CLIENT_ID`` /
  ``LEASE_ID`` (the namespace variable name is configurable via ``instance.namespace_env``);
* ``rclpy.init`` -> warmup (first ``<prefix>/joint_states`` sample, 30 s default) ->
  publish ``<ns>/inference/stopped=True`` at 2 Hz (RELIABLE + TRANSIENT_LOCAL depth 1);
* subscribe ``<ns>/env/reset_status`` and start rounds on a strict False->True edge
  (continuously latched True never starts another round; resets received while a round
  runs are dropped);
* one round = publish ``stopped=False`` -> build a
  :class:`policy_base.policy.backend.RoundCommand` with
  :func:`policy_base.policy.backend.build_round_command` -> run it as a child process ->
  read its report -> publish ``stopped=True``;
* publish the ``<prefix>/joint_command`` hold heartbeat (latest measured joint positions,
  2-5 Hz) while idle and between rounds, strictly paused while a policy child runs so the
  child owns the arm traffic;
* SIGTERM/SIGINT -> terminate the child (SIGTERM -> 5 s grace -> SIGKILL), stop timers,
  ``rclpy.shutdown`` and exit 0; a failed round logs ``stage``/``error``/report fields and
  exits 1; config/env/warmup/ROS-dependency failures exit 2.

Safety: the instance never calls arm authority acquire/release services (the factory
manager owns the lease). ``--dry-run`` forces the offline mock backend inside
:func:`policy_base.policy.backend.build_round_command`; real-motion rounds additionally
require ``policy.allow_platform_execution`` + ``--enable-motion`` + ``--confirm-real``,
enforced by the policy backend (never bypassed here). No ROS import happens at module
import time, so the failure paths and unit tests work without a sourced ROS 2 environment.
"""

from __future__ import annotations

import argparse
import os
import signal
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from policy_base.instance.config import (
    InstanceConfig,
    InstanceSettings,
    load_instance_config,
)
from policy_base.instance.keepalive import (
    MAX_HOLD_HZ,
    MIN_HOLD_HZ,
    HoldCommand,
    Keepalive,
)
from policy_base.instance.protocol import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_RUNTIME,
    InstanceEnv,
    InstanceProtocol,
    event_line,
    exit_code_for_round,
    parse_instance_env,
)
from policy_base.instance.runner import (
    DEFAULT_STOP_GRACE_S,
    ChildOutcome,
    RoundRunner,
)
from policy_base.policy.backend import build_round_command, summarize_round_report

#: Warmup budget while waiting for the first ``<prefix>/joint_states`` sample.
DEFAULT_WARMUP_TIMEOUT_S = 30.0
#: Leave >1 stopped publish period for the final ``stopped=True`` to be delivered
#: before a failed instance exits.
FAILURE_PUBLISH_DRAIN_S = 0.7
#: QoS depth for the measured joint feedback and hold command topics.
FEEDBACK_QOS_DEPTH = 10
SPIN_PERIOD_S = 0.05
#: rclpy spin slice used while a child round runs (keeps stopped/SIGTERM responsive).
ROUND_SPIN_PERIOD_S = 0.02


def emit(name: str, **fields: Any) -> None:
    """Print one structured JSON log line to stdout (asserted by tests/manager logs)."""
    print(event_line(name, **fields), flush=True)


@dataclass(frozen=True)
class RuntimeOptions:
    """Effective per-process timings after config defaults and CLI overrides."""

    warmup_timeout_s: float
    round_timeout_s: float
    hold_hz: float
    stop_grace_s: float


class _Shutdown:
    """SIGTERM/SIGINT latch shared by the main loop and the child runner."""

    def __init__(self) -> None:
        self.requested = False
        self.signum: int | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the instance CLI (validation of provided overrides; config fills defaults)."""
    parser = argparse.ArgumentParser(
        prog="policy_base.instance.entry",
        description="policy_base ACT instance entry (IM instance contract)",
    )
    parser.add_argument("--config", type=Path, required=True, help="policy_base instance YAML")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="force the offline mock policy backend; tests only, never on hardware",
    )
    parser.add_argument(
        "--enable-motion",
        action="store_true",
        help=(
            "allow real arm motion (also requires --confirm-real and "
            "PolicyConfig.allow_platform_execution)"
        ),
    )
    parser.add_argument(
        "--confirm-real",
        action="store_true",
        help="explicit on-hardware confirmation gate (never bypassable by the entry)",
    )
    parser.add_argument(
        "--warmup-timeout",
        type=float,
        default=None,
        metavar="S",
        help="seconds to wait for the first <prefix>/joint_states sample (default: config value)",
    )
    parser.add_argument(
        "--round-timeout",
        type=float,
        default=None,
        metavar="S",
        help="single policy-round child timeout in seconds (default: config value)",
    )
    parser.add_argument(
        "--hold-hz",
        type=float,
        default=None,
        metavar="HZ",
        help="hold heartbeat rate, must be within 2-5 Hz (default: config value)",
    )
    parser.add_argument(
        "--stop-grace",
        type=float,
        default=DEFAULT_STOP_GRACE_S,
        metavar="S",
        help="SIGTERM->SIGKILL grace for the child at shutdown (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    if args.warmup_timeout is not None and args.warmup_timeout <= 0:
        parser.error("--warmup-timeout must be positive")
    if args.round_timeout is not None and args.round_timeout <= 0:
        parser.error("--round-timeout must be positive")
    if args.hold_hz is not None and not MIN_HOLD_HZ <= args.hold_hz <= MAX_HOLD_HZ:
        parser.error("--hold-hz must be within 2-5 Hz (instance contract)")
    if args.stop_grace < 0:
        parser.error("--stop-grace must not be negative")
    return args


def resolve_runtime_options(args: argparse.Namespace, settings: InstanceSettings) -> RuntimeOptions:
    """Resolve CLI overrides against the instance config defaults (validated upstream)."""
    return RuntimeOptions(
        warmup_timeout_s=(
            settings.warmup_timeout_s if args.warmup_timeout is None else float(args.warmup_timeout)
        ),
        round_timeout_s=(
            settings.round_timeout_s if args.round_timeout is None else float(args.round_timeout)
        ),
        hold_hz=settings.hold_hz if args.hold_hz is None else float(args.hold_hz),
        stop_grace_s=float(args.stop_grace),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: config/env resolution, then the rclpy serve loop (exit code)."""
    args = parse_args(argv)
    try:
        config = load_instance_config(args.config)
    except Exception as exc:  # every config/loading failure is exit code 2
        emit("instance_failed", stage="config", error=f"{type(exc).__name__}: {exc}")
        return EXIT_CONFIG
    try:
        env = parse_instance_env(os.environ, namespace_env=config.instance.namespace_env)
    except ValueError as exc:
        emit("instance_failed", stage="environment", error=str(exc))
        return EXIT_CONFIG
    for warning in env.warnings:
        emit("instance_warning", warning=warning)
    options = resolve_runtime_options(args, config.instance)
    try:
        keepalive = Keepalive(config.ros2.joint_names, hz=options.hold_hz)
    except ValueError as exc:
        emit("instance_failed", stage="config", error=str(exc))
        return EXIT_CONFIG
    # The instance contract requires DOMAIN_ID; when the process did not inherit
    # ROS_DOMAIN_ID, derive it from DOMAIN_ID before rclpy.init reads it.
    if not (os.environ.get("ROS_DOMAIN_ID") or "").strip():
        os.environ["ROS_DOMAIN_ID"] = str(env.domain_id)
    emit(
        "instance_starting",
        namespace=env.namespace,
        domain_id=env.domain_id,
        station_type=env.station_type,
        client_id=env.client_id,
        lease_id=env.lease_id,
        inference_id=env.inference_id,
        dry_run=bool(args.dry_run),
        enable_motion=bool(args.enable_motion),
        confirm_real=bool(args.confirm_real),
        backend=config.policy.backend,
        config=str(args.config.expanduser().resolve()),
        warmup_timeout_s=options.warmup_timeout_s,
        round_timeout_s=options.round_timeout_s,
        hold_hz=options.hold_hz,
        stop_grace_s=options.stop_grace_s,
    )
    return _serve(args, env, config, keepalive, options)


def _serve(
    args: argparse.Namespace,
    env: InstanceEnv,
    config: InstanceConfig,
    keepalive: Keepalive,
    options: RuntimeOptions,
) -> int:
    """Initialize rclpy and run the instance protocol loop; all ROS imports stay here."""
    try:
        import rclpy  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on the shell environment
        emit(
            "instance_failed",
            stage="ros2_init",
            error=f"rclpy is unavailable ({exc}); source the ROS 2 environment",
        )
        return EXIT_CONFIG
    from rclpy.executors import (  # type: ignore[import-not-found]
        ExternalShutdownException,
        SingleThreadedExecutor,
    )
    from rclpy.node import Node  # type: ignore[import-not-found]
    from rclpy.signals import SignalHandlerOptions  # type: ignore[import-not-found]
    from sensor_msgs.msg import JointState  # type: ignore[import-not-found]
    from std_msgs.msg import Bool  # type: ignore[import-not-found]

    shutdown = _Shutdown()
    try:
        runner = RoundRunner(
            timeout_s=options.round_timeout_s,
            stop_grace_s=options.stop_grace_s,
        )
    except ValueError as exc:
        emit("instance_failed", stage="config", error=str(exc))
        return EXIT_CONFIG

    def _handle_signal(signum: int, _frame: Any) -> None:
        shutdown.requested = True
        shutdown.signum = signum
        runner.interrupt()

    previous_handlers: dict[int, Any] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous_handlers[signum] = signal.signal(signum, _handle_signal)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass
    try:
        rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    except Exception as exc:  # noqa: BLE001 - ROS init failure = configuration error
        emit("instance_failed", stage="ros2_init", error=f"{type(exc).__name__}: {exc}")
        _restore_signal_handlers(previous_handlers)
        return EXIT_CONFIG

    node = None
    executor = None
    app: _InstanceApp | None = None
    report_dir: tempfile.TemporaryDirectory[str] | None = None
    try:
        node = Node(f"policy_base_instance_{os.getpid()}")
        executor = SingleThreadedExecutor()
        executor.add_node(node)
        report_dir = tempfile.TemporaryDirectory(prefix="policy_base_instance_rounds_")
        app = _InstanceApp(
            node=node,
            executor=executor,
            config=config,
            env=env,
            keepalive=keepalive,
            runner=runner,
            report_dir=Path(report_dir.name),
            shutdown=shutdown,
            warmup_timeout_s=options.warmup_timeout_s,
            enable_motion=bool(args.enable_motion),
            confirm_real=bool(args.confirm_real),
            dry_run=bool(args.dry_run),
            rclpy_module=rclpy,
            external_shutdown=ExternalShutdownException,
            joint_state_type=JointState,
            bool_type=Bool,
        )
        return app.run()
    except Exception as exc:  # noqa: BLE001 - never leak a running child on crashes
        emit(
            "instance_failed",
            stage="runtime",
            error=f"{type(exc).__name__}: {exc}",
            inference_id=env.inference_id,
        )
        return EXIT_RUNTIME
    finally:
        runner.interrupt()
        if app is not None:
            app.close()
        if executor is not None and node is not None:
            try:
                executor.remove_node(node)
            except Exception:  # pragma: no cover - best-effort teardown
                pass
            try:
                executor.shutdown()
            except Exception:  # pragma: no cover
                pass
        if node is not None:
            try:
                node.destroy_node()
            except Exception:  # pragma: no cover
                pass
        try:
            rclpy.shutdown()
        except Exception:  # pragma: no cover - already shut down
            pass
        if report_dir is not None:
            report_dir.cleanup()
        _restore_signal_handlers(previous_handlers)


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass


class _InstanceApp:
    """rclpy wiring for one instance process (stopped/reset/hold + policy round loop)."""

    def __init__(
        self,
        *,
        node: Any,
        executor: Any,
        config: InstanceConfig,
        env: InstanceEnv,
        keepalive: Keepalive,
        runner: Any,
        report_dir: Path,
        shutdown: _Shutdown,
        warmup_timeout_s: float,
        enable_motion: bool,
        confirm_real: bool,
        dry_run: bool,
        rclpy_module: Any,
        external_shutdown: type[BaseException],
        joint_state_type: type[Any],
        bool_type: type[Any],
    ) -> None:
        self.node = node
        self.executor = executor
        self.config = config
        self.env = env
        self.keepalive = keepalive
        self.runner = runner
        self.report_dir = Path(report_dir)
        self.shutdown = shutdown
        self.warmup_timeout_s = float(warmup_timeout_s)
        self.enable_motion = bool(enable_motion)
        self.confirm_real = bool(confirm_real)
        self.dry_run = bool(dry_run)
        self.rclpy = rclpy_module
        self._external_shutdown = external_shutdown
        self._joint_state_type = joint_state_type
        self._bool_type = bool_type
        self.gate = InstanceProtocol()
        self._round_requested = False
        self._stopped_timer: Any = None
        self._hold_timer: Any = None
        self._closed = False

        from rclpy.qos import (  # type: ignore[import-not-found]
            DurabilityPolicy,
            HistoryPolicy,
            QoSProfile,
            ReliabilityPolicy,
        )

        stopped_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        reset_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        feedback_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=FEEDBACK_QOS_DEPTH,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        ros2 = config.ros2
        self.stopped_pub = node.create_publisher(bool_type, env.stopped_topic, stopped_qos)
        self.hold_pub = node.create_publisher(
            joint_state_type, ros2.joint_command_topic, feedback_qos
        )
        node.create_subscription(
            bool_type, env.reset_status_topic, self._on_reset_status, reset_qos
        )
        node.create_subscription(
            joint_state_type, ros2.joint_states_topic, self._on_joint_states, feedback_qos
        )
        # Initial latched level: stopped=True until the first round edge.
        self._publish_stopped(True)
        self._stopped_timer = node.create_timer(
            1.0 / config.instance.stopped_hz, self._on_stopped_timer
        )
        self._hold_timer = node.create_timer(self.keepalive.period_s, self._on_hold_timer)
        emit(
            "instance_topics",
            stopped=env.stopped_topic,
            reset_status=env.reset_status_topic,
            joint_states=ros2.joint_states_topic,
            joint_command=ros2.joint_command_topic,
            prefix=ros2.prefix,
            stopped_hz=config.instance.stopped_hz,
            hold_hz=self.keepalive.hz,
        )

    # -- ROS callbacks -----------------------------------------------------
    def _on_reset_status(self, message: Any) -> None:
        if self.gate.on_reset_status(bool(message.data)):
            self._round_requested = True

    def _on_joint_states(self, message: Any) -> None:
        self.keepalive.on_joint_states(message.name, message.position)

    def _on_stopped_timer(self) -> None:
        if self.shutdown.requested:
            return
        self._publish_stopped(self.gate.stopped_value)

    def _on_hold_timer(self) -> None:
        if self.shutdown.requested:
            return
        command = self.keepalive.next_command()
        if command is None:
            return
        self._publish_hold(command)

    # -- helpers -----------------------------------------------------------
    def _publish_stopped(self, value: bool) -> None:
        self.stopped_pub.publish(self._bool_type(data=bool(value)))

    def _publish_hold(self, command: HoldCommand) -> None:
        message = self._joint_state_type()
        message.header.stamp = self.node.get_clock().now().to_msg()
        message.name = list(command.names)
        message.position = [float(value) for value in command.positions]
        self.hold_pub.publish(message)

    def _spin_once(self, timeout_s: float) -> None:
        if not self.rclpy.ok():
            return
        try:
            self.executor.spin_once(timeout_sec=timeout_s)
        except self._external_shutdown:  # pragma: no cover - external rclpy shutdown
            self.shutdown.requested = True

    def _drain(self, duration_s: float) -> None:
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            if self.shutdown.requested or not self.rclpy.ok():
                return
            self._spin_once(SPIN_PERIOD_S)

    # -- lifecycle ---------------------------------------------------------
    def run(self) -> int:
        try:
            warmup_code = self._warmup()
            if warmup_code is not None:
                return warmup_code
            emit(
                "instance_ready",
                namespace=self.env.namespace,
                inference_id=self.env.inference_id,
                stopped_topic=self.env.stopped_topic,
                reset_status_topic=self.env.reset_status_topic,
                joint_states_topic=self.config.ros2.joint_states_topic,
                joint_command_topic=self.config.ros2.joint_command_topic,
                hold_hz=self.keepalive.hz,
            )
            self.gate.mark_ready()
            self._publish_stopped(True)
            self.keepalive.start()
            while not self.shutdown.requested:
                if not self.rclpy.ok():  # pragma: no cover - external shutdown
                    break
                self._spin_once(SPIN_PERIOD_S)
                if self._round_requested and not self.shutdown.requested:
                    self._round_requested = False
                    code = self._run_round()
                    if code is not None:
                        return code
            emit(
                "instance_stopping",
                signal=self.shutdown.signum,
                rounds_completed=self.gate.rounds_completed,
                rounds_failed=self.gate.rounds_failed,
                discarded_resets=self.gate.discarded_resets,
            )
            return EXIT_OK
        finally:
            self.close()

    def _warmup(self) -> int | None:
        """Wait for the first joint_states sample (connection check before serving)."""
        deadline = time.monotonic() + self.warmup_timeout_s
        while time.monotonic() < deadline:
            if self.shutdown.requested:
                emit("instance_stopping", signal=self.shutdown.signum, stage="warmup")
                return EXIT_OK
            if not self.rclpy.ok():  # pragma: no cover - external shutdown
                return EXIT_RUNTIME
            self._spin_once(SPIN_PERIOD_S)
            if self.keepalive.has_sample:
                return None
        emit(
            "instance_failed",
            stage="warmup",
            error=(
                f"no sample on {self.config.ros2.joint_states_topic} within "
                f"{self.warmup_timeout_s:g}s"
            ),
            inference_id=self.env.inference_id,
        )
        return EXIT_CONFIG

    def _round_fields(self, outcome: ChildOutcome) -> dict[str, object]:
        """Event fields shared by ``round_finished`` / ``instance_failed``.

        The frozen report contract tolerates a missing/corrupt report, so the summary is
        always present (backend defaults when ``outcome.report is None``); the process
        exit code stays authoritative for pass/fail, so the outcome status is used when
        the report carries no status.
        """
        summary = summarize_round_report(outcome.report)
        fields: dict[str, object] = dict(summary)
        fields["status"] = summary.get("status") or outcome.status
        fields["returncode"] = outcome.returncode
        return fields

    def _fail_round(self, round_number: int, *, stage: str, error: str) -> int:
        """Classify an unexpected build/runner failure as a failed round (exit 1 or 0)."""
        self.gate.on_round_finished(failed=True)
        emit(
            "instance_failed",
            stage=stage,
            error=error,
            inference_id=self.env.inference_id,
            round=round_number,
            returncode=None,
        )
        self._publish_stopped(True)
        self._drain(FAILURE_PUBLISH_DRAIN_S)
        if self.shutdown.requested:
            return EXIT_OK
        return EXIT_RUNTIME

    def _run_round(self) -> int | None:
        """Run one policy child round; return None to keep serving or the instance exit code."""
        round_number = self.gate.rounds_started
        self.keepalive.stop()
        self._publish_stopped(False)
        emit(
            "round_started",
            round=round_number,
            inference_id=self.env.inference_id,
            dry_run=self.dry_run,
            enable_motion=self.enable_motion,
            confirm_real=self.confirm_real,
        )
        report_path = self.report_dir / f"round_{round_number:04d}.json"
        try:
            command = build_round_command(
                self.config.policy,
                report_path=report_path,
                enable_motion=self.enable_motion,
                confirm_real=self.confirm_real,
                dry_run=self.dry_run,
            )
        except Exception as exc:  # backend safety gate / config failure -> failed round
            return self._fail_round(
                round_number, stage="backend", error=f"{type(exc).__name__}: {exc}"
            )
        try:
            outcome = self.runner.run(command, on_tick=lambda: self._spin_once(ROUND_SPIN_PERIOD_S))
        except Exception as exc:  # an unexpected runner bug still fails cleanly (no leak)
            return self._fail_round(
                round_number, stage="round", error=f"{type(exc).__name__}: {exc}"
            )
        exit_code = exit_code_for_round(outcome.status)
        fields = self._round_fields(outcome)
        if exit_code is None:
            self.gate.on_round_finished()
            self._publish_stopped(True)
            self.keepalive.start()
            emit(
                "round_finished",
                round=round_number,
                inference_id=self.env.inference_id,
                rounds_completed=self.gate.rounds_completed,
                **fields,
            )
            return None
        self.gate.on_round_finished(failed=exit_code != EXIT_OK)
        if exit_code == EXIT_OK:
            emit(
                "round_interrupted",
                round=round_number,
                inference_id=self.env.inference_id,
                signal=self.shutdown.signum,
                **fields,
            )
            return EXIT_OK
        emit(
            "instance_failed",
            stage=outcome.stage,
            error=outcome.error or outcome.status,
            inference_id=self.env.inference_id,
            round=round_number,
            exit_code=outcome.returncode,
            **fields,
        )
        self._publish_stopped(True)
        self._drain(FAILURE_PUBLISH_DRAIN_S)
        if self.shutdown.requested:
            return EXIT_OK
        return EXIT_RUNTIME

    def close(self) -> None:
        """Idempotent teardown: stop the hold heartbeat and the published timers."""
        if self._closed:
            return
        self._closed = True
        self.keepalive.stop()
        for timer in (self._stopped_timer, self._hold_timer):
            if timer is None:
                continue
            try:
                timer.cancel()
            except Exception:  # pragma: no cover - best-effort teardown
                pass


if __name__ == "__main__":
    raise SystemExit(main())
