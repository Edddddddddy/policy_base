"""Instance YAML configuration for the policy_base instance entry (strict, no ROS imports).

The instance config has three sections (``schema: so101.policy_base.instance_config.v1``):

.. code-block:: yaml

   schema: so101.policy_base.instance_config.v1
   instance:
     namespace_env: INFERENCE_NAMESPACE     # env var carrying the manager namespace
     warmup_timeout_s: 30.0                 # wait for the first <prefix>/joint_states
     round_timeout_s: 600.0                 # one policy child-budget
     hold_hz: 4.0                           # idle hold heartbeat, must stay within 2-5 Hz
     stopped_hz: 2.0                        # <ns>/inference/stopped level rate
   ros2:
     prefix: /arm
     joint_names: [shoulder_pan, shoulder_lift, elbow_flex, wrist_flex, wrist_roll, gripper]
   policy:
     backend: finetune                      # parsed by policy_base.policy.config

This module only validates/parses the instance-owned sections and derives the
``<prefix>/joint_states`` / ``<prefix>/joint_command`` topics. The ``policy:`` section is
delegated verbatim to :func:`policy_base.policy.config.load_policy_config` with
``base_dir`` set to the repository root (relative checkpoints resolve there). No ROS,
torch or lerobot import happens here, so the loader is unit testable offline.

Invalid YAML/schema/values raise :class:`InstanceConfigError` (a ``ValueError``); the
entry maps every such failure to exit code 2. Nothing is written or created on disk.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from policy_base.instance.keepalive import MAX_HOLD_HZ, MIN_HOLD_HZ
from policy_base.instance.protocol import DEFAULT_NAMESPACE_ENV, validate_env_name

if TYPE_CHECKING:
    from policy_base.policy.config import PolicyConfig

#: Frozen instance configuration schema identifier (unknown schema -> exit code 2).
INSTANCE_SCHEMA = "so101.policy_base.instance_config.v1"

#: Frozen six-joint SO-101 order (last joint is the gripper); shared with rule_base.
DEFAULT_JOINT_NAMES: tuple[str, ...] = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
)
DEFAULT_ROS2_PREFIX = "/arm"
DEFAULT_WARMUP_TIMEOUT_S = 30.0
DEFAULT_ROUND_TIMEOUT_S = 600.0
DEFAULT_STOPPED_HZ = 2.0
#: Sanity cap for the configured stopped level rate (the frozen contract uses 2 Hz).
MAX_STOPPED_HZ = 10.0

_TOP_LEVEL_KEYS = frozenset({"schema", "instance", "ros2", "policy"})
_INSTANCE_KEYS = frozenset(
    {"namespace_env", "warmup_timeout_s", "round_timeout_s", "hold_hz", "stopped_hz"}
)
_ROS2_KEYS = frozenset({"prefix", "joint_names"})


class InstanceConfigError(ValueError):
    """Invalid/missing instance configuration file (entry maps it to exit code 2)."""


@dataclass(frozen=True)
class InstanceSettings:
    """Instance-owned protocol timing/namespace settings."""

    namespace_env: str = DEFAULT_NAMESPACE_ENV
    warmup_timeout_s: float = DEFAULT_WARMUP_TIMEOUT_S
    round_timeout_s: float = DEFAULT_ROUND_TIMEOUT_S
    hold_hz: float = 4.0
    stopped_hz: float = DEFAULT_STOPPED_HZ


@dataclass(frozen=True)
class Ros2Settings:
    """ROS 2 channel names used by the instance control/hold wiring."""

    prefix: str = DEFAULT_ROS2_PREFIX
    joint_names: tuple[str, ...] = DEFAULT_JOINT_NAMES

    @property
    def joint_states_topic(self) -> str:
        return f"{self.prefix}/joint_states"

    @property
    def joint_command_topic(self) -> str:
        return f"{self.prefix}/joint_command"


@dataclass(frozen=True)
class InstanceConfig:
    """Parsed instance YAML (immutable): instance timings, ROS 2 topics, policy section."""

    instance: InstanceSettings
    ros2: Ros2Settings
    policy: PolicyConfig


def repo_root() -> Path:
    """This checkout's repository root (``config.py`` lives at ``<root>/src/.../instance``)."""
    return Path(__file__).resolve().parents[3]


def _require_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InstanceConfigError(f"{name} must be a YAML mapping, got {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            raise InstanceConfigError(f"{name} keys must be strings, got {key!r}")
    return value


def _reject_unknown(mapping: Mapping[str, object], allowed: frozenset[str], name: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise InstanceConfigError(
            f"{name} has unknown field(s) {unknown}; allowed fields are {sorted(allowed)}"
        )


def _as_float(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InstanceConfigError(f"{name} must be a number, got {value!r}")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise InstanceConfigError(f"{name} must be finite, got {value!r}")
    return numeric


def _as_positive_float(name: str, value: object) -> float:
    numeric = _as_float(name, value)
    if numeric <= 0:
        raise InstanceConfigError(f"{name} must be positive, got {value!r}")
    return numeric


def _as_str(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise InstanceConfigError(f"{name} must be a string, got {type(value).__name__}")
    return value


def _parse_instance_settings(section: Mapping[str, object]) -> InstanceSettings:
    _reject_unknown(section, _INSTANCE_KEYS, "instance")
    raw_namespace_env = section.get("namespace_env", DEFAULT_NAMESPACE_ENV)
    namespace_env = _as_str("instance.namespace_env", raw_namespace_env)
    try:
        validate_env_name(namespace_env)
    except ValueError as exc:
        raise InstanceConfigError(f"instance.namespace_env is invalid: {exc}") from exc
    warmup_timeout_s = _as_positive_float(
        "instance.warmup_timeout_s", section.get("warmup_timeout_s", DEFAULT_WARMUP_TIMEOUT_S)
    )
    round_timeout_s = _as_positive_float(
        "instance.round_timeout_s", section.get("round_timeout_s", DEFAULT_ROUND_TIMEOUT_S)
    )
    hold_hz = _as_float("instance.hold_hz", section.get("hold_hz", 4.0))
    if not MIN_HOLD_HZ <= hold_hz <= MAX_HOLD_HZ:
        raise InstanceConfigError(
            f"instance.hold_hz={hold_hz:g} must be within [{MIN_HOLD_HZ:g}, {MAX_HOLD_HZ:g}] Hz "
            "(instance contract)"
        )
    stopped_hz = _as_float("instance.stopped_hz", section.get("stopped_hz", DEFAULT_STOPPED_HZ))
    if not 0 < stopped_hz <= MAX_STOPPED_HZ:
        raise InstanceConfigError(
            f"instance.stopped_hz={stopped_hz:g} must be in (0, {MAX_STOPPED_HZ:g}] Hz "
            "(instance contract: 2 Hz)"
        )
    return InstanceSettings(
        namespace_env=namespace_env,
        warmup_timeout_s=warmup_timeout_s,
        round_timeout_s=round_timeout_s,
        hold_hz=hold_hz,
        stopped_hz=stopped_hz,
    )


def _validate_prefix(prefix: str) -> str:
    if (
        not prefix.startswith("/")
        or prefix == "/"
        or prefix.endswith("/")
        or "//" in prefix
        or any(char.isspace() for char in prefix)
    ):
        raise InstanceConfigError(
            f"ros2.prefix={prefix!r} must be an absolute ROS name such as /arm "
            "(no empty segments, no whitespace)"
        )
    return prefix


def _validate_joint_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise InstanceConfigError("ros2.joint_names must be a YAML list of strings")
    names: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item or any(char.isspace() for char in item):
            raise InstanceConfigError(
                f"ros2.joint_names[{index}] must be a non-blank string without whitespace"
            )
        names.append(item)
    if len(names) != len(DEFAULT_JOINT_NAMES):
        raise InstanceConfigError(
            f"ros2.joint_names must hold exactly {len(DEFAULT_JOINT_NAMES)} SO-101 joints, "
            f"got {len(names)}"
        )
    if len(set(names)) != len(names):
        raise InstanceConfigError("ros2.joint_names must be distinct")
    return tuple(names)


def _parse_ros2_settings(section: Mapping[str, object]) -> Ros2Settings:
    _reject_unknown(section, _ROS2_KEYS, "ros2")
    prefix = _validate_prefix(_as_str("ros2.prefix", section.get("prefix", DEFAULT_ROS2_PREFIX)))
    raw_joint_names = section.get("joint_names", list(DEFAULT_JOINT_NAMES))
    return Ros2Settings(prefix=prefix, joint_names=_validate_joint_names(raw_joint_names))


def _load_policy_section(section: Mapping[str, object]) -> PolicyConfig:
    """Delegate the ``policy:`` section to the frozen ACT policy config loader."""
    from policy_base.policy.config import load_policy_config

    try:
        return load_policy_config(section, base_dir=repo_root())
    except ValueError as exc:
        raise InstanceConfigError(f"policy section is invalid: {exc}") from exc


def load_instance_config(path: Path | str) -> InstanceConfig:
    """Load and strictly validate one instance YAML file (no ROS, no writes).

    Raises :class:`InstanceConfigError` (a ``ValueError``) for unreadable files, invalid
    YAML, a wrong/missing schema, unknown fields or out-of-range values.
    """
    config_path = Path(path).expanduser()
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstanceConfigError(f"cannot read instance config {config_path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise InstanceConfigError(f"invalid YAML in instance config {config_path}: {exc}") from exc
    document = _require_mapping(data, "instance config root")
    _reject_unknown(document, _TOP_LEVEL_KEYS, "instance config root")

    schema = document.get("schema")
    if schema != INSTANCE_SCHEMA:
        raise InstanceConfigError(
            f"instance config schema must be {INSTANCE_SCHEMA!r}, got {schema!r}"
        )
    raw_instance = _require_mapping(document.get("instance"), "instance section")
    raw_ros2 = _require_mapping(document.get("ros2"), "ros2 section")
    raw_policy = _require_mapping(document.get("policy"), "policy section")

    return InstanceConfig(
        instance=_parse_instance_settings(raw_instance),
        ros2=_parse_ros2_settings(raw_ros2),
        policy=_load_policy_section(raw_policy),
    )
