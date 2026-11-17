"""finetune 复用辅助单测（commit pin / checkpoint 解析 / deploy argv / git HEAD）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from policy_base.policy.config import FinetuneSettings, load_policy_config
from policy_base.policy.finetune import (
    COMMIT_PIN_MAX_LENGTH,
    COMMIT_PIN_MIN_LENGTH,
    FINETUNE_DEPLOY_SUBCOMMAND,
    FINETUNE_EXECUTE_MAX_DURATION_S,
    FINETUNE_EXECUTE_MIN_DURATION_S,
    FINETUNE_REUSE_SCHEMA,
    FINETUNE_RUNTIME_CONFIG_SCHEMA,
    REPO_ROOT,
    FinetuneReuseError,
    build_deploy_execute_argv,
    commit_matches_pin,
    read_git_head,
    resolve_checkpoint_path,
    validate_commit_pin,
    validate_execute_duration_s,
)

_FULL_HEAD = "7f6da5c" + "a" * 33


def test_reuse_contract_constants() -> None:
    assert FINETUNE_REUSE_SCHEMA == "so101.policy_base.finetune_reuse.v1"
    assert FINETUNE_DEPLOY_SUBCOMMAND == "execute"
    assert FINETUNE_RUNTIME_CONFIG_SCHEMA == "so101.act.runtime_config.v1"
    assert (FINETUNE_EXECUTE_MIN_DURATION_S, FINETUNE_EXECUTE_MAX_DURATION_S) == (1, 300)
    assert (COMMIT_PIN_MIN_LENGTH, COMMIT_PIN_MAX_LENGTH) == (7, 40)


def test_repo_root_points_at_repository() -> None:
    assert (REPO_ROOT / "pyproject.toml").is_file()
    assert (REPO_ROOT / "src" / "policy_base" / "policy" / "finetune.py").is_file()


# ---------------------------------------------------------------------------
# commit pin
# ---------------------------------------------------------------------------
def test_validate_commit_pin_normalizes() -> None:
    assert validate_commit_pin("7f6da5c") == "7f6da5c"
    assert validate_commit_pin(" 7F6DA5C ") == "7f6da5c"
    assert validate_commit_pin("a" * 40) == "a" * 40


@pytest.mark.parametrize("pin", ["abc123", "g123456", "", "a" * 41])
def test_validate_commit_pin_rejects_invalid(pin: str) -> None:
    with pytest.raises(FinetuneReuseError, match="commit pin"):
        validate_commit_pin(pin)


def test_validate_commit_pin_rejects_non_string() -> None:
    with pytest.raises(FinetuneReuseError, match="string"):
        validate_commit_pin(123)  # type: ignore[arg-type]


def test_commit_matches_pin_prefix() -> None:
    assert commit_matches_pin(_FULL_HEAD, "7f6da5c")
    assert commit_matches_pin(_FULL_HEAD.upper(), "7F6DA5C")
    assert commit_matches_pin(_FULL_HEAD, _FULL_HEAD)
    assert not commit_matches_pin(_FULL_HEAD, "abcdef0")
    with pytest.raises(FinetuneReuseError):
        commit_matches_pin(_FULL_HEAD, "not-hex")


# ---------------------------------------------------------------------------
# checkpoint 路径解析
# ---------------------------------------------------------------------------
def test_resolve_checkpoint_absolute() -> None:
    assert resolve_checkpoint_path("/models/act/pretrained_model", repo_root=Path("/repo")) == (
        Path("/models/act/pretrained_model")
    )


def test_resolve_checkpoint_relative() -> None:
    assert resolve_checkpoint_path("models/act", repo_root=Path("/repo")) == Path(
        "/repo/models/act"
    )


def test_resolve_checkpoint_none_or_empty() -> None:
    assert resolve_checkpoint_path(None, repo_root=Path("/repo")) is None
    assert resolve_checkpoint_path("", repo_root=Path("/repo")) is None


def test_resolve_checkpoint_expands_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_checkpoint_path("~/ckpt", repo_root=Path("/repo")) == tmp_path / "ckpt"


# ---------------------------------------------------------------------------
# 轮次时长
# ---------------------------------------------------------------------------
def test_validate_execute_duration_truncates() -> None:
    assert validate_execute_duration_s(60.0) == 60
    assert validate_execute_duration_s(42.9) == 42
    assert validate_execute_duration_s(1) == 1
    assert validate_execute_duration_s(300) == 300


@pytest.mark.parametrize("duration", [0.0, 0.5, 301.0, float("nan"), float("inf"), True, "60"])
def test_validate_execute_duration_rejects_invalid(duration: object) -> None:
    with pytest.raises(FinetuneReuseError, match="max_duration_s"):
        validate_execute_duration_s(duration)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# deploy argv
# ---------------------------------------------------------------------------
def test_build_deploy_execute_argv_contract() -> None:
    settings = FinetuneSettings(repo=Path("/opt/finetune"), venv_python="/opt/venv/bin/python")
    argv = build_deploy_execute_argv(settings, max_duration_s=60.0, extra_args=("--json",))
    assert argv == (
        "bash",
        "/opt/finetune/deploy/run.sh",
        "execute",
        "--config",
        "configs/runtime_execute.yaml",
        "--enable-motion",
        "--confirm-real",
        "--max-duration-s",
        "60",
        "--json",
    )


def test_build_deploy_execute_argv_no_extra_args() -> None:
    settings = FinetuneSettings(repo=Path("/opt/finetune"))
    argv = build_deploy_execute_argv(settings, max_duration_s=1)
    assert argv[-2:] == ("--max-duration-s", "1")
    assert len(argv) == 9


def test_build_deploy_execute_argv_honors_overrides() -> None:
    settings = FinetuneSettings(
        repo=Path("/r"), deploy_script="custom/run.sh", runtime_config="cfg/execute.yaml"
    )
    argv = build_deploy_execute_argv(settings, max_duration_s=30)
    assert argv[1] == "/r/custom/run.sh"
    assert argv[4] == "cfg/execute.yaml"


def test_build_deploy_execute_argv_rejects_invalid_duration() -> None:
    settings = FinetuneSettings(repo=Path("/r"))
    with pytest.raises(FinetuneReuseError):
        build_deploy_execute_argv(settings, max_duration_s=0.0)


# ---------------------------------------------------------------------------
# git HEAD（只读）
# ---------------------------------------------------------------------------
def test_read_git_head_of_policy_base_repo() -> None:
    head = read_git_head(REPO_ROOT)
    assert head is not None
    assert len(head) == 40
    assert set(head) <= set("0123456789abcdef")


def test_read_git_head_returns_none_for_non_repo(tmp_path: Path) -> None:
    assert read_git_head(tmp_path) is None
    assert read_git_head(tmp_path / "missing") is None


# ---------------------------------------------------------------------------
# 与配置解析的衔接
# ---------------------------------------------------------------------------
def test_config_finetune_settings_feed_deploy_argv(tmp_path: Path) -> None:
    config = load_policy_config(
        {
            "backend": "finetune",
            "checkpoint": "models/act/pretrained_model",
            "max_duration_s": 45.0,
            "finetune": {"repo": "/opt/finetune", "extra_args": ["--json"]},
        },
        base_dir=tmp_path,
    )
    assert config.finetune is not None
    argv = build_deploy_execute_argv(
        config.finetune,
        max_duration_s=config.max_duration_s,
        extra_args=config.finetune.extra_args,
    )
    assert argv[1] == "/opt/finetune/deploy/run.sh"
    assert argv[-3:] == ("--max-duration-s", "45", "--json")
