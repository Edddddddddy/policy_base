"""Instance protocol for the policy_base IM entry (rule_base instance contract preserved).

This module is plain Python: environment parsing, namespace-to-topic resolution, the
``/env/reset_status`` False->True edge state machine and the exit-code classification are
all testable without a sourced ROS 2 environment. The ROS wiring lives in
:mod:`policy_base.instance.entry`; the policy subprocess wiring lives in
:mod:`policy_base.policy.backend` / :mod:`policy_base.instance.runner`.

Frozen semantics (shared with the rule_base instance mode, see
``policy_base/docs/instance-mode.md``):

* the reset gate arms only after observing at least one ``False`` level sample;
* the first ``True`` while armed is one new reset edge -> consume it, start one policy
  round, disarm the gate;
* reset levels received while a round runs are dropped (never queued/replayed);
* after a round the gate requires a fresh ``False`` before the next round, so a
  continuously latched ``True`` level never starts another round.

policy_base differs from rule_base only in what a round executes (an ACT policy
subprocess built by :mod:`policy_base.policy.backend` instead of the rule_base node
CLI); these protocol semantics are frozen and must stay compatible.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Exit codes frozen by the IM instance contract: SIGTERM -> 0, round failure -> 1,
# config/env -> 2 (identical to rule_base instance mode).
EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2

#: Only station type supported by this repository in instance mode.
SUPPORTED_STATION_TYPES: tuple[str, ...] = ("pick_place",)

#: Instance control-plane topic suffixes injected by the inference manager.
STOPPED_SUFFIX = "inference/stopped"
RESET_STATUS_SUFFIX = "env/reset_status"

#: Manager-injected environment variable carrying the ROS namespace.
DEFAULT_NAMESPACE_ENV = "INFERENCE_NAMESPACE"

# Reset-gate states.
STATE_WARMING = "warming"
STATE_IDLE = "idle"
STATE_RUNNING = "running"

# Child-round outcome statuses shared with :mod:`policy_base.instance.runner`.
CHILD_OK = "ok"
CHILD_FAILED = "failed"
CHILD_TIMEOUT = "timeout"
CHILD_INTERRUPTED = "interrupted"
CHILD_SPAWN_ERROR = "spawn_error"


class InstanceConfigError(ValueError):
    """Raised for invalid/missing instance environment/configuration (exit code 2)."""


@dataclass(frozen=True)
class InstanceEnv:
    """Parsed manager-injected environment (same fields as the rule_base contract)."""

    namespace: str
    domain_id: int
    station_type: str
    client_id: str
    lease_id: str
    inference_id: int | None = None
    warnings: tuple[str, ...] = ()

    @property
    def stopped_topic(self) -> str:
        return namespace_topic(self.namespace, STOPPED_SUFFIX)

    @property
    def reset_status_topic(self) -> str:
        return namespace_topic(self.namespace, RESET_STATUS_SUFFIX)


def validate_namespace(namespace: str) -> None:
    """Validate ``INFERENCE_NAMESPACE``: empty or an absolute ROS name."""
    if namespace == "":
        return
    if not namespace.startswith("/"):
        raise InstanceConfigError(
            f"INFERENCE_NAMESPACE={namespace!r} must be empty or an absolute name starting "
            "with '/' (instance contract)"
        )
    if namespace == "/" or namespace.endswith("/") or "//" in namespace:
        raise InstanceConfigError(
            f"INFERENCE_NAMESPACE={namespace!r} must not contain empty name segments "
            "(instance contract)"
        )
    if any(char.isspace() for char in namespace):
        raise InstanceConfigError(
            f"INFERENCE_NAMESPACE={namespace!r} must not contain whitespace (instance contract)"
        )


def namespace_topic(namespace: str, suffix: str) -> str:
    """Resolve a control-plane topic: empty namespace -> ``/<suffix>`` else ``<ns>/<suffix>``."""
    clean_suffix = suffix.strip("/")
    if not clean_suffix:
        raise ValueError("topic suffix must not be empty")
    if namespace == "":
        return f"/{clean_suffix}"
    return f"{namespace.rstrip('/')}/{clean_suffix}"


def validate_env_name(name: str) -> None:
    """Validate a configured environment-variable name (``instance.namespace_env``)."""
    if not name:
        raise InstanceConfigError("namespace environment variable name must not be empty")
    if not (name[0].isalpha() or name[0] == "_"):
        raise InstanceConfigError(
            f"namespace environment variable name {name!r} must start with a letter or '_'"
        )
    if not all(char.isalnum() or char == "_" for char in name):
        raise InstanceConfigError(
            f"namespace environment variable name {name!r} may only contain letters, digits and '_'"
        )


def _parse_int(name: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise InstanceConfigError(f"{name}={raw!r} is not an integer (instance contract)") from None


def parse_instance_env(
    environ: Mapping[str, str] | None = None,
    *,
    namespace_env: str = DEFAULT_NAMESPACE_ENV,
) -> InstanceEnv:
    """Parse and validate the manager-injected environment.

    ``DOMAIN_ID`` and ``STATION_TYPE`` are mandatory; a set ``ROS_DOMAIN_ID`` must agree
    with ``DOMAIN_ID`` (the entry writes ``ROS_DOMAIN_ID`` from ``DOMAIN_ID`` before
    ``rclpy.init`` when the process did not inherit one). Missing ``CLIENT_ID`` /
    ``LEASE_ID`` are warnings only (local dev); ``INFERENCE_ID`` is log-only. The
    namespace is read from ``namespace_env`` (``INFERENCE_NAMESPACE`` by default; the
    instance YAML may name another variable for test harnesses).
    """
    env = os.environ if environ is None else environ
    validate_env_name(namespace_env)
    warnings: list[str] = []

    raw_namespace = env.get(namespace_env)
    if raw_namespace is None:
        warnings.append(
            f"{namespace_env} was not injected; defaulting to '' (instance contract requires "
            "manager injection)"
        )
        namespace = ""
    else:
        namespace = raw_namespace.strip()
    validate_namespace(namespace)

    raw_domain = (env.get("DOMAIN_ID") or "").strip()
    if not raw_domain:
        raise InstanceConfigError("DOMAIN_ID is required (instance contract)")
    domain_id = _parse_int("DOMAIN_ID", raw_domain)
    if domain_id < 0:
        raise InstanceConfigError(f"DOMAIN_ID={domain_id} must not be negative (instance contract)")

    raw_ros_domain = (env.get("ROS_DOMAIN_ID") or "").strip()
    if raw_ros_domain:
        ros_domain = _parse_int("ROS_DOMAIN_ID", raw_ros_domain)
        if ros_domain != domain_id:
            raise InstanceConfigError(
                f"DOMAIN_ID={domain_id} does not match ROS_DOMAIN_ID={ros_domain} "
                "(instance contract: they must agree)"
            )

    station_type = (env.get("STATION_TYPE") or "").strip()
    if not station_type:
        raise InstanceConfigError("STATION_TYPE is required (instance contract)")
    if station_type not in SUPPORTED_STATION_TYPES:
        supported = ", ".join(SUPPORTED_STATION_TYPES)
        raise InstanceConfigError(
            f"STATION_TYPE={station_type!r} is not supported; expected one of: {supported}"
        )

    client_id = (env.get("CLIENT_ID") or "").strip()
    lease_id = (env.get("LEASE_ID") or "").strip()
    if not client_id:
        warnings.append(
            "CLIENT_ID is missing/empty (local dev may run without it; the manager must "
            "inject it in production)"
        )
    if not lease_id:
        warnings.append(
            "LEASE_ID is missing/empty (local dev may run without it; the manager must "
            "inject it in production)"
        )

    inference_id: int | None = None
    raw_inference_id = (env.get("INFERENCE_ID") or "").strip()
    if raw_inference_id:
        try:
            inference_id = int(raw_inference_id)
        except ValueError:
            warnings.append(
                f"INFERENCE_ID={raw_inference_id!r} is not an integer; ignored (log only)"
            )

    return InstanceEnv(
        namespace=namespace,
        domain_id=domain_id,
        station_type=station_type,
        client_id=client_id,
        lease_id=lease_id,
        inference_id=inference_id,
        warnings=tuple(warnings),
    )


class InstanceProtocol:
    """``/env/reset_status`` False->True edge gate (frozen instance contract)."""

    def __init__(self) -> None:
        self.state = STATE_WARMING
        self.armed = False
        self.rounds_started = 0
        self.rounds_completed = 0
        self.rounds_failed = 0
        self.discarded_resets = 0

    @property
    def running(self) -> bool:
        return self.state == STATE_RUNNING

    @property
    def stopped_value(self) -> bool:
        """Value published on ``<ns>/inference/stopped``: False only while running."""
        return self.state != STATE_RUNNING

    def mark_ready(self) -> None:
        """Finish warmup: start accepting reset levels."""
        if self.state == STATE_WARMING:
            self.state = STATE_IDLE
        self.armed = False

    def on_reset_status(self, value: bool) -> bool:
        """Feed one ``/env/reset_status`` sample; return True iff a new round must start."""
        if self.state == STATE_WARMING:
            return False
        if self.state == STATE_RUNNING:
            # Resets received during a round are dropped, never queued/replayed.
            self.discarded_resets += 1
            return False
        if not value:
            # A False level arms the gate (needed before any True edge).
            self.armed = True
            return False
        if not self.armed:
            # A latched/continuous True level without a fresh False is not an edge.
            return False
        # Consume exactly one True edge, start the round, disarm.
        self.armed = False
        self.state = STATE_RUNNING
        self.rounds_started += 1
        return True

    def on_round_finished(self, *, failed: bool = False) -> None:
        """Mark the running round as finished; re-arm only after a fresh False."""
        if self.state != STATE_RUNNING:
            raise RuntimeError(f"round finished while not running (state={self.state!r})")
        self.state = STATE_IDLE
        self.armed = False
        self.rounds_completed += 1
        if failed:
            self.rounds_failed += 1


def exit_code_for_round(status: str) -> int | None:
    """Map a child-round outcome to the instance exit code.

    ``None`` means "keep serving and wait for the next reset edge".
    """
    if status == CHILD_OK:
        return None
    if status == CHILD_INTERRUPTED:
        return EXIT_OK
    return EXIT_RUNTIME


def event_line(name: str, **fields: Any) -> str:
    """Render one structured JSON log line for stdout (kept pure for unit tests)."""
    payload: dict[str, Any] = {"event": name}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False, default=str)
