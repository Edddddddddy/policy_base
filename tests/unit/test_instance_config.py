"""Purpose: unit-test the strict instance YAML loader (schema/values/topics, no ROS).

The ``policy:`` section is owned by the policy backend; here the frozen
``policy_base.policy.config.load_policy_config`` loader is monkeypatched so these tests
only exercise the instance-owned parsing contract.

Usage: env -u PYTHONPATH .venv/bin/python -m pytest tests/unit/test_instance_config.py -q
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from policy_base.instance.config import (
    DEFAULT_JOINT_NAMES,
    INSTANCE_SCHEMA,
    InstanceConfig,
    InstanceConfigError,
    InstanceSettings,
    Ros2Settings,
    load_instance_config,
    repo_root,
)
from policy_base.policy.config import PolicyConfig

BASE_YAML = """\
schema: so101.policy_base.instance_config.v1
instance:
  namespace_env: INFERENCE_NAMESPACE
  warmup_timeout_s: 12.0
  round_timeout_s: 34.0
  hold_hz: 4.5
  stopped_hz: 2.0
ros2:
  prefix: /arm
  joint_names: [shoulder_pan, shoulder_lift, elbow_flex, wrist_flex, wrist_roll, gripper]
policy:
  backend: mock
"""


def write_config(tmp_path: Path, text: str = BASE_YAML, name: str = "instance.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def policy_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, Path]]:
    """Patch the frozen policy loader and record ``(section, base_dir)`` calls."""
    calls: list[tuple[object, Path]] = []

    def fake_load(section: object, *, base_dir: Path) -> PolicyConfig:
        calls.append((section, base_dir))
        backend = "mock"
        if isinstance(section, dict) and isinstance(section.get("backend"), str):
            backend = str(section["backend"])
        return PolicyConfig(backend=backend)

    monkeypatch.setattr("policy_base.policy.config.load_policy_config", fake_load)
    return calls


# --- happy path ------------------------------------------------------------
def test_loads_instance_sections_and_topics(tmp_path, policy_calls):
    config = load_instance_config(write_config(tmp_path))
    assert isinstance(config, InstanceConfig)
    assert config.instance == InstanceSettings(
        namespace_env="INFERENCE_NAMESPACE",
        warmup_timeout_s=12.0,
        round_timeout_s=34.0,
        hold_hz=4.5,
        stopped_hz=2.0,
    )
    assert config.ros2 == Ros2Settings(prefix="/arm", joint_names=DEFAULT_JOINT_NAMES)
    assert config.ros2.joint_states_topic == "/arm/joint_states"
    assert config.ros2.joint_command_topic == "/arm/joint_command"
    assert config.policy.backend == "mock"


def test_parsed_config_is_immutable(tmp_path, policy_calls):
    config = load_instance_config(write_config(tmp_path))
    with pytest.raises(FrozenInstanceError):
        config.instance = InstanceSettings()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        config.ros2.prefix = "/other"  # type: ignore[misc]


def test_policy_section_is_forwarded_to_the_frozen_loader(tmp_path, policy_calls):
    path = write_config(tmp_path)
    load_instance_config(path)
    assert len(policy_calls) == 1
    section, base_dir = policy_calls[0]
    assert isinstance(section, dict)
    assert section["backend"] == "mock"
    assert base_dir == repo_root()


def test_repo_root_is_this_checkout(tmp_path, policy_calls):
    root = repo_root()
    assert (root / "pyproject.toml").is_file()
    assert (root / "src/policy_base/instance/config.py").is_file()


def test_missing_instance_fields_use_defaults(tmp_path, policy_calls):
    yaml_text = """\
schema: so101.policy_base.instance_config.v1
instance: {}
ros2: {}
policy:
  backend: mock
"""
    config = load_instance_config(write_config(tmp_path, yaml_text))
    assert config.instance == InstanceSettings()
    assert config.ros2.prefix == "/arm"
    assert config.ros2.joint_names == DEFAULT_JOINT_NAMES


def test_load_accepts_string_path(tmp_path, policy_calls):
    path = write_config(tmp_path)
    assert load_instance_config(str(path)).ros2.prefix == "/arm"


def test_load_does_not_modify_the_config_file(tmp_path, policy_calls):
    path = write_config(tmp_path)
    before = path.read_text(encoding="utf-8")
    load_instance_config(path)
    assert path.read_text(encoding="utf-8") == before


# --- file / document failures ---------------------------------------------
def test_missing_file_is_a_value_error(tmp_path):
    with pytest.raises(InstanceConfigError, match="cannot read instance config"):
        load_instance_config(tmp_path / "does-not-exist.yaml")
    assert issubclass(InstanceConfigError, ValueError)


def test_invalid_yaml_is_rejected(tmp_path):
    path = write_config(tmp_path, "schema: [unclosed\n")
    with pytest.raises(InstanceConfigError, match="invalid YAML"):
        load_instance_config(path)


def test_non_mapping_document_is_rejected(tmp_path):
    path = write_config(tmp_path, "- just\n- a list\n")
    with pytest.raises(InstanceConfigError, match="root"):
        load_instance_config(path)


def test_empty_document_is_rejected(tmp_path):
    path = write_config(tmp_path, "")
    with pytest.raises(InstanceConfigError, match="root"):
        load_instance_config(path)


def test_missing_schema_is_rejected(tmp_path):
    path = write_config(tmp_path, BASE_YAML.replace(f"schema: {INSTANCE_SCHEMA}\n", ""))
    with pytest.raises(InstanceConfigError, match="schema"):
        load_instance_config(path)


def test_wrong_schema_is_rejected(tmp_path):
    path = write_config(
        tmp_path, BASE_YAML.replace(INSTANCE_SCHEMA, "so101.policy_base.instance_config.v2")
    )
    with pytest.raises(InstanceConfigError, match="schema"):
        load_instance_config(path)


def test_unknown_top_level_field_is_rejected(tmp_path):
    path = write_config(tmp_path, BASE_YAML + "extra: true\n")
    with pytest.raises(InstanceConfigError, match="unknown field"):
        load_instance_config(path)


@pytest.mark.parametrize(
    ("section", "text"),
    [
        (
            "instance",
            "schema: so101.policy_base.instance_config.v1\n"
            "instance: not-a-map\n"
            "ros2: {}\n"
            "policy: {backend: mock}\n",
        ),
        (
            "ros2",
            "schema: so101.policy_base.instance_config.v1\n"
            "instance: {}\n"
            "ros2: not-a-map\n"
            "policy: {backend: mock}\n",
        ),
        (
            "policy",
            "schema: so101.policy_base.instance_config.v1\n"
            "instance: {}\n"
            "ros2: {}\n"
            "policy: not-a-map\n",
        ),
    ],
)
def test_each_required_section_must_be_a_mapping(tmp_path, section, text):
    path = write_config(tmp_path, text)
    with pytest.raises(InstanceConfigError, match=f"{section} section"):
        load_instance_config(path)


def test_missing_policy_section_is_rejected(tmp_path):
    text = BASE_YAML.split("policy:")[0]
    path = write_config(tmp_path, text)
    with pytest.raises(InstanceConfigError, match="policy section"):
        load_instance_config(path)


def test_policy_loader_value_error_is_wrapped(tmp_path, monkeypatch):
    def failing_load(section: object, *, base_dir: Path) -> PolicyConfig:
        raise ValueError("backend must be finetune or mock")

    monkeypatch.setattr("policy_base.policy.config.load_policy_config", failing_load)
    with pytest.raises(InstanceConfigError, match="policy section is invalid"):
        load_instance_config(write_config(tmp_path))


# --- instance section validation ------------------------------------------
def test_unknown_instance_field_is_rejected(tmp_path, policy_calls):
    text = BASE_YAML.replace("  stopped_hz: 2.0\n", "  stopped_hz: 2.0\n  bogus: 1\n")
    with pytest.raises(InstanceConfigError, match="unknown field"):
        load_instance_config(write_config(tmp_path, text))


def test_invalid_namespace_env_names_are_rejected(tmp_path, policy_calls):
    for bad in ("''", "1BAD", "BAD NAME", "bad-name"):
        text = BASE_YAML.replace("namespace_env: INFERENCE_NAMESPACE", f"namespace_env: {bad}")
        with pytest.raises(InstanceConfigError):
            load_instance_config(write_config(tmp_path, text))


@pytest.mark.parametrize(
    "field,value",
    [
        ("warmup_timeout_s", "0"),
        ("warmup_timeout_s", "-1"),
        ("warmup_timeout_s", "true"),
        ("warmup_timeout_s", "'30'"),
        ("round_timeout_s", "0"),
        ("round_timeout_s", "null"),
        ("hold_hz", "1.99"),
        ("hold_hz", "5.01"),
        ("hold_hz", "'4'"),
        ("stopped_hz", "0"),
        ("stopped_hz", "-2"),
        ("stopped_hz", "10.1"),
        ("stopped_hz", "'2'"),
    ],
)
def test_invalid_instance_values_are_rejected(tmp_path, policy_calls, field, value):
    lines = []
    for line in BASE_YAML.splitlines():
        if line.strip().startswith(f"{field}:"):
            lines.append(f"  {field}: {value}")
        else:
            lines.append(line)
    with pytest.raises(InstanceConfigError):
        load_instance_config(write_config(tmp_path, "\n".join(lines) + "\n"))


def test_non_finite_instance_values_are_rejected(tmp_path, policy_calls):
    text = BASE_YAML.replace("warmup_timeout_s: 12.0", "warmup_timeout_s: .nan")
    with pytest.raises(InstanceConfigError, match="finite"):
        load_instance_config(write_config(tmp_path, text))


# --- ros2 section validation ----------------------------------------------
def test_unknown_ros2_field_is_rejected(tmp_path, policy_calls):
    text = BASE_YAML.replace("ros2:\n", "ros2:\n  base_frame: base_link\n")
    with pytest.raises(InstanceConfigError, match="unknown field"):
        load_instance_config(write_config(tmp_path, text))


@pytest.mark.parametrize(
    "prefix",
    ["arm", "/", "/arm/", "/a//b", "/a b", "''"],
)
def test_invalid_ros2_prefix_is_rejected(tmp_path, policy_calls, prefix):
    text = BASE_YAML.replace("prefix: /arm", f"prefix: {prefix}")
    with pytest.raises(InstanceConfigError, match="ros2.prefix"):
        load_instance_config(write_config(tmp_path, text))


@pytest.mark.parametrize(
    "joints",
    [
        "[shoulder_pan, shoulder_lift]",
        "[a, b, c, d, e, f, g]",
        "[a, b, c, d, e, e]",
        "[a, b, c, d, e, '']",
        "[a, b, c, d, e, 'bad name']",
        "[a, b, c, d, e, 6]",
        "not-a-list",
    ],
)
def test_invalid_ros2_joint_names_are_rejected(tmp_path, policy_calls, joints):
    lines = []
    for line in BASE_YAML.splitlines():
        if line.strip().startswith("joint_names:"):
            lines.append(f"  joint_names: {joints}")
        else:
            lines.append(line)
    with pytest.raises(InstanceConfigError, match="joint_names"):
        load_instance_config(write_config(tmp_path, "\n".join(lines) + "\n"))


def test_custom_prefix_and_joint_order_derive_topics(tmp_path, policy_calls):
    text = BASE_YAML.replace("prefix: /arm", "prefix: /leader")
    config = load_instance_config(write_config(tmp_path, text))
    assert config.ros2.joint_states_topic == "/leader/joint_states"
    assert config.ros2.joint_command_topic == "/leader/joint_command"


def test_namespace_env_can_name_a_custom_variable(tmp_path, policy_calls):
    text = BASE_YAML.replace("namespace_env: INFERENCE_NAMESPACE", "namespace_env: POLICY_NS")
    config = load_instance_config(write_config(tmp_path, text))
    assert config.instance.namespace_env == "POLICY_NS"
