"""``policy:`` 配置段解析单测（纯离线；不连 ROS/网络、不加载权重）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from policy_base.policy.config import (
    BACKENDS,
    FinetuneSettings,
    MockSettings,
    PolicyConfig,
    PolicyConfigError,
    expand_env_reference,
    load_policy_config,
)


# ---------------------------------------------------------------------------
# expand_env_reference
# ---------------------------------------------------------------------------
def test_expand_env_reference_reads_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLICY_TEST_CKPT", "/srv/ckpt")
    assert expand_env_reference("${POLICY_TEST_CKPT}") == "/srv/ckpt"


def test_expand_env_reference_reads_explicit_environ() -> None:
    assert expand_env_reference("${CKPT}", environ={"CKPT": "/from/mapping"}) == "/from/mapping"


def test_expand_env_reference_uses_default_when_unset() -> None:
    assert expand_env_reference("${POLICY_TEST_MISSING:-/fallback}", environ={}) == "/fallback"
    assert expand_env_reference("${POLICY_TEST_MISSING:-}", environ={}) == ""


def test_expand_env_reference_uses_default_when_empty() -> None:
    assert expand_env_reference("${EMPTY:-fallback}", environ={"EMPTY": ""}) == "fallback"


def test_expand_env_reference_unset_without_default_raises() -> None:
    with pytest.raises(PolicyConfigError, match="POLICY_TEST_MISSING"):
        expand_env_reference("${POLICY_TEST_MISSING}", environ={})


def test_expand_env_reference_leaves_non_references_alone() -> None:
    assert expand_env_reference("models/act/pretrained_model") == "models/act/pretrained_model"
    assert expand_env_reference("$NOT_A_REFERENCE") == "$NOT_A_REFERENCE"
    assert expand_env_reference("~/models") == "~/models"


def test_expand_env_reference_handles_embedded_reference() -> None:
    assert expand_env_reference("prefix-${NAME}/suffix", environ={"NAME": "x"}) == "prefix-x/suffix"


# ---------------------------------------------------------------------------
# load_policy_config: 正常路径
# ---------------------------------------------------------------------------
def _live_section(**overrides: object) -> dict[str, object]:
    section: dict[str, object] = {
        "backend": "finetune",
        "checkpoint": "models/act-20261117-act-wrist-smoke/pretrained_model",
        "allow_platform_execution": True,
        "max_duration_s": 60.0,
        "finetune": {
            "repo": "/home/ubuntu/yayyrobotics/finetune",
            "deploy_script": "deploy/run.sh",
            "runtime_config": "configs/runtime_execute.yaml",
            "venv_python": "${SO101_VENV_PYTHON:-/home/ubuntu/discover/so101/.venv/bin/python}",
            "device": "cuda",
            "commit": "7f6da5c",
            "extra_args": ["--json"],
        },
        "mock": {"steps": 30, "chunk_size": 5, "step_delay_s": 0.0, "seed": 0},
    }
    section.update(overrides)
    return section


def test_load_finetune_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SO101_VENV_PYTHON", raising=False)
    config = load_policy_config(_live_section(), base_dir=tmp_path)
    assert isinstance(config, PolicyConfig)
    assert config.backend == "finetune"
    assert config.checkpoint == str(
        tmp_path / "models/act-20261117-act-wrist-smoke/pretrained_model"
    )
    assert config.allow_platform_execution is True
    assert config.max_duration_s == 60.0
    assert config.finetune == FinetuneSettings(
        repo=Path("/home/ubuntu/yayyrobotics/finetune"),
        deploy_script="deploy/run.sh",
        runtime_config="configs/runtime_execute.yaml",
        venv_python="/home/ubuntu/discover/so101/.venv/bin/python",
        device="cuda",
        commit="7f6da5c",
        extra_args=("--json",),
    )
    assert config.mock == MockSettings(steps=30, chunk_size=5, step_delay_s=0.0, seed=0)


def test_load_mock_config_uses_defaults(tmp_path: Path) -> None:
    config = load_policy_config({"backend": "mock"}, base_dir=tmp_path)
    assert config == PolicyConfig(backend="mock")
    assert config.mock == MockSettings()
    assert config.finetune is None


def test_load_mock_config_accepts_env_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POLICY_TEST_CKPT", str(tmp_path / "env" / "pretrained_model"))
    config = load_policy_config(
        {"backend": "mock", "checkpoint": "${POLICY_TEST_CKPT}"}, base_dir=tmp_path
    )
    assert config.checkpoint == str(tmp_path / "env" / "pretrained_model")


def test_load_config_keeps_absolute_checkpoint_and_expands_default(
    tmp_path: Path,
) -> None:
    config = load_policy_config(
        {
            "backend": "mock",
            "checkpoint": "${POLICY_TEST_UNSET:-/abs/pretrained_model}",
        },
        base_dir=tmp_path,
    )
    assert config.checkpoint == "/abs/pretrained_model"


def test_load_finetune_resolves_relative_repo(tmp_path: Path) -> None:
    config = load_policy_config(
        {"backend": "finetune", "finetune": {"repo": "finetune"}}, base_dir=tmp_path
    )
    assert config.finetune is not None
    assert config.finetune.repo == tmp_path / "finetune"
    assert config.finetune.deploy_script == "deploy/run.sh"
    assert config.finetune.runtime_config == "configs/runtime_execute.yaml"
    assert config.finetune.venv_python == ""
    assert config.finetune.device == "cuda"
    assert config.finetune.commit is None
    assert config.finetune.extra_args == ()


def test_load_allows_finetune_section_with_mock_backend(tmp_path: Path) -> None:
    """mock 后端可携带（按同一严格规则解析的）finetune 段，便于切换后端。"""
    config = load_policy_config(
        {"backend": "mock", "finetune": {"repo": "/abs/finetune"}}, base_dir=tmp_path
    )
    assert config.backend == "mock"
    assert config.finetune == FinetuneSettings(repo=Path("/abs/finetune"))


def test_load_expands_extra_args_env_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POLICY_TEST_ARG", "--device=cpu")
    config = load_policy_config(
        {
            "backend": "finetune",
            "finetune": {"repo": "/abs/finetune", "extra_args": ["${POLICY_TEST_ARG}"]},
        },
        base_dir=tmp_path,
    )
    assert config.finetune is not None
    assert config.finetune.extra_args == ("--device=cpu",)


# ---------------------------------------------------------------------------
# load_policy_config: 错误路径
# ---------------------------------------------------------------------------
def test_load_checkpoint_env_unset_raises(tmp_path: Path) -> None:
    with pytest.raises(PolicyConfigError, match="POLICY_TEST_CKPT"):
        load_policy_config(
            {"backend": "mock", "checkpoint": "${POLICY_TEST_CKPT}"}, base_dir=tmp_path
        )


@pytest.mark.parametrize(
    ("section", "match"),
    [
        ({}, "backend is required"),
        ({"backend": "ros2"}, "policy.backend"),
        ({"backend": "mock", "unknown": 1}, "unknown field"),
        ({"backend": "mock", "mock": {"steps": 0}}, "steps"),
        ({"backend": "mock", "mock": {"chunk_size": 0}}, "chunk_size"),
        ({"backend": "mock", "mock": {"step_delay_s": -0.1}}, "step_delay_s"),
        ({"backend": "mock", "mock": {"steps": 1.5}}, "integer"),
        ({"backend": "mock", "mock": {"unknown": 1}}, "unknown field"),
        ({"backend": "finetune"}, "policy.finetune section is required"),
        ({"backend": "finetune", "finetune": {}}, "repo is required"),
        ({"backend": "finetune", "finetune": {"repo": "/x", "commit": "not-hex"}}, "commit"),
        ({"backend": "finetune", "finetune": {"repo": "/x", "extra_args": [1]}}, "extra_args"),
        ({"backend": "finetune", "finetune": {"repo": "/x", "deploy_script": ""}}, "deploy_script"),
        ({"backend": "mock", "finetune": "oops"}, "mapping"),
        ({"backend": "mock", "max_duration_s": 0}, "max_duration_s"),
        ({"backend": "mock", "max_duration_s": "60"}, "number"),
        ({"backend": "mock", "allow_platform_execution": "true"}, "boolean"),
        ({"backend": "mock", "checkpoint": 5}, "string"),
        ({"backend": "mock", "checkpoint": ""}, "must not be empty"),
    ],
)
def test_load_invalid_config_raises(section: object, match: str, tmp_path: Path) -> None:
    with pytest.raises(PolicyConfigError, match=match):
        load_policy_config(section, base_dir=tmp_path)


@pytest.mark.parametrize("section", [None, [], "policy", 3])
def test_load_non_mapping_section_raises(section: object, tmp_path: Path) -> None:
    with pytest.raises(PolicyConfigError, match="must be a mapping"):
        load_policy_config(section, base_dir=tmp_path)


def test_backends_constant_matches_contract() -> None:
    assert set(BACKENDS) == {"finetune", "mock"}
