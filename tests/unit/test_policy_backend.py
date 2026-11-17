"""轮次命令构建/报告解析单测（纯离线；不启动子进程、不发运动命令）。"""

from __future__ import annotations

import ast
import dataclasses
import json
import sys
from pathlib import Path

import pytest

from policy_base.policy.backend import (
    ROUND_REPORT_SCHEMA,
    PolicyBackendError,
    build_round_command,
    read_round_report,
    summarize_round_report,
)
from policy_base.policy.config import FinetuneSettings, MockSettings, PolicyConfig

_REPORT = Path("/tmp/policy_base_test/round_report.json")
_POLICY_SOURCE_DIR = Path(__file__).resolve().parents[2] / "src" / "policy_base" / "policy"


def _mock_policy() -> PolicyConfig:
    return PolicyConfig(
        backend="mock",
        mock=MockSettings(steps=7, chunk_size=3, step_delay_s=0.25, seed=11),
    )


def _finetune_policy() -> PolicyConfig:
    return PolicyConfig(
        backend="finetune",
        checkpoint="/models/act/pretrained_model",
        allow_platform_execution=True,
        max_duration_s=60.0,
        finetune=FinetuneSettings(
            repo=Path("/opt/finetune"),
            deploy_script="deploy/run.sh",
            runtime_config="configs/runtime_execute.yaml",
            venv_python="/opt/venv/bin/python",
            device="cuda",
            commit="abc1234",
            extra_args=("--json",),
        ),
    )


def _expected_mock_argv(policy: PolicyConfig, report_path: Path) -> tuple[str, ...]:
    mock = policy.mock
    return (
        sys.executable,
        "-m",
        "policy_base.policy.mock",
        "--report",
        str(report_path),
        "--steps",
        str(mock.steps),
        "--chunk-size",
        str(mock.chunk_size),
        "--step-delay-s",
        str(mock.step_delay_s),
        "--seed",
        str(mock.seed),
    )


# ---------------------------------------------------------------------------
# mock 命令 / dry-run 强制 mock
# ---------------------------------------------------------------------------
def test_mock_backend_command_contract() -> None:
    policy = _mock_policy()
    command = build_round_command(policy, report_path=_REPORT)
    assert command.argv == _expected_mock_argv(policy, _REPORT)
    assert command.env == {}
    assert command.cwd is None
    assert command.report_path == _REPORT
    assert command.description == "mock"


def test_dry_run_forces_mock_even_for_finetune_backend() -> None:
    policy = _finetune_policy()
    command = build_round_command(policy, report_path=_REPORT, dry_run=True)
    assert command.description == "mock"
    assert command.argv == _expected_mock_argv(policy, _REPORT)
    assert command.cwd is None
    assert command.env == {}


def test_dry_run_ignores_finetune_safety_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = dataclasses.replace(_finetune_policy(), allow_platform_execution=False)
    command = build_round_command(policy, report_path=_REPORT, dry_run=True)
    assert command.description == "mock"
    assert "--enable-motion" not in command.argv
    assert "--confirm-real" not in command.argv


# ---------------------------------------------------------------------------
# finetune 安全门
# ---------------------------------------------------------------------------
def test_finetune_requires_allow_platform_execution() -> None:
    policy = dataclasses.replace(_finetune_policy(), allow_platform_execution=False)
    with pytest.raises(PolicyBackendError, match="allow_platform_execution"):
        build_round_command(policy, report_path=_REPORT, enable_motion=True, confirm_real=True)


def test_finetune_requires_enable_motion() -> None:
    with pytest.raises(PolicyBackendError, match="--enable-motion"):
        build_round_command(
            _finetune_policy(), report_path=_REPORT, enable_motion=False, confirm_real=True
        )


def test_finetune_requires_confirm_real() -> None:
    with pytest.raises(PolicyBackendError, match="--confirm-real"):
        build_round_command(
            _finetune_policy(), report_path=_REPORT, enable_motion=True, confirm_real=False
        )


def test_finetune_reports_every_missing_gate() -> None:
    policy = dataclasses.replace(_finetune_policy(), allow_platform_execution=False)
    with pytest.raises(PolicyBackendError) as excinfo:
        build_round_command(policy, report_path=_REPORT)
    message = str(excinfo.value)
    assert "allow_platform_execution" in message
    assert "--enable-motion" in message
    assert "--confirm-real" in message


def test_finetune_requires_checkpoint() -> None:
    policy = dataclasses.replace(_finetune_policy(), checkpoint=None)
    with pytest.raises(PolicyBackendError, match="checkpoint"):
        build_round_command(policy, report_path=_REPORT, enable_motion=True, confirm_real=True)


def test_finetune_requires_settings() -> None:
    policy = dataclasses.replace(_finetune_policy(), finetune=None)
    with pytest.raises(PolicyBackendError, match="policy.finetune"):
        build_round_command(policy, report_path=_REPORT, enable_motion=True, confirm_real=True)


def test_unsupported_backend_raises() -> None:
    policy = dataclasses.replace(_mock_policy(), backend="ros2")
    with pytest.raises(PolicyBackendError, match="unsupported policy backend"):
        build_round_command(policy, report_path=_REPORT)


# ---------------------------------------------------------------------------
# finetune 命令契约
# ---------------------------------------------------------------------------
def test_finetune_command_contract() -> None:
    command = build_round_command(
        _finetune_policy(), report_path=_REPORT, enable_motion=True, confirm_real=True
    )
    assert command.argv == (
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
    assert command.env == {
        "SO101_CHECKPOINT": "/models/act/pretrained_model",
        "SO101_VENV_PYTHON": "/opt/venv/bin/python",
        "SO101_DEVICE": "cuda",
    }
    assert command.cwd == Path("/opt/finetune")
    assert command.report_path == _REPORT
    assert command.description == "finetune"


def test_finetune_command_truncates_duration() -> None:
    policy = dataclasses.replace(_finetune_policy(), max_duration_s=42.9)
    command = build_round_command(
        policy, report_path=_REPORT, enable_motion=True, confirm_real=True
    )
    assert command.argv[command.argv.index("--max-duration-s") + 1] == "42"


@pytest.mark.parametrize("max_duration_s", [0.5, 301.0])
def test_finetune_rejects_out_of_range_duration(max_duration_s: float) -> None:
    policy = dataclasses.replace(_finetune_policy(), max_duration_s=max_duration_s)
    with pytest.raises(PolicyBackendError, match="max_duration_s"):
        build_round_command(policy, report_path=_REPORT, enable_motion=True, confirm_real=True)


def test_finetune_does_not_inject_empty_env_values() -> None:
    policy = dataclasses.replace(
        _finetune_policy(),
        finetune=FinetuneSettings(repo=Path("/opt/finetune"), venv_python="", device=""),
    )
    command = build_round_command(
        policy, report_path=_REPORT, enable_motion=True, confirm_real=True
    )
    assert command.env == {"SO101_CHECKPOINT": "/models/act/pretrained_model"}


# ---------------------------------------------------------------------------
# 轮次报告读取 / 精简
# ---------------------------------------------------------------------------
def test_read_round_report_reads_mapping(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    payload = {"schema": ROUND_REPORT_SCHEMA, "status": "ok", "steps": 3}
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    assert read_round_report(report_path) == payload


def test_read_round_report_returns_none_for_missing_and_invalid(tmp_path: Path) -> None:
    assert read_round_report(None) is None
    assert read_round_report(tmp_path / "missing.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert read_round_report(broken) is None
    non_mapping = tmp_path / "list.json"
    non_mapping.write_text("[1, 2]", encoding="utf-8")
    assert read_round_report(non_mapping) is None
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    assert read_round_report(empty) is None


def test_summarize_round_report_full_report() -> None:
    report = {
        "schema": ROUND_REPORT_SCHEMA,
        "status": "ok",
        "backend": "mock",
        "checkpoint": None,
        "steps": 10,
        "published": 10,
        "duration_s": 0.25,
        "chunks": 4,
        "error": None,
    }
    assert summarize_round_report(report) == {
        "backend": "mock",
        "steps": 10,
        "published": 10,
        "duration_s": 0.25,
        "status": "ok",
        "checkpoint": None,
    }


def test_summarize_round_report_none_gives_defaults() -> None:
    assert summarize_round_report(None) == {
        "backend": None,
        "steps": 0,
        "published": 0,
        "duration_s": 0.0,
        "status": None,
        "checkpoint": None,
    }


def test_summarize_round_report_tolerates_malformed_values() -> None:
    summarized = summarize_round_report(
        {
            "backend": 5,
            "steps": "ten",
            "published": -3,
            "duration_s": float("nan"),
            "status": "",
            "checkpoint": {"path": "x"},
        }
    )
    assert summarized == {
        "backend": None,
        "steps": 0,
        "published": 0,
        "duration_s": 0.0,
        "status": None,
        "checkpoint": None,
    }
    assert json.dumps(summarized)


def test_summarize_round_report_keeps_checkpoint_text() -> None:
    summarized = summarize_round_report({"status": "failed", "checkpoint": "/models/act"})
    assert summarized["status"] == "failed"
    assert summarized["checkpoint"] == "/models/act"
    assert json.dumps(summarized)


# ---------------------------------------------------------------------------
# 依赖边界：策略包不得 import ROS/torch/lerobot
# ---------------------------------------------------------------------------
def test_policy_modules_do_not_import_heavy_runtime_dependencies() -> None:
    forbidden_roots = {"torch", "lerobot", "rclpy", "so101_act_runtime", "so101_act_server"}
    modules = sorted(_POLICY_SOURCE_DIR.glob("*.py"))
    assert modules
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported: list[str]
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            else:
                continue
            for name in imported:
                assert name.split(".")[0] not in forbidden_roots, f"{path.name} imports {name}"
