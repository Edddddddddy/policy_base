"""离线预检 CLI 单测（合成 checkpoint + 真实 checkpoint 可用时补跑；不连网/不加载权重）。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from policy_base.policy import preflight
from policy_base.policy.preflight import main

_REPO_ROOT = Path(__file__).resolve().parents[2]
#: 真实 checkpoint 目录候选（Lead/DVC 可能带 ``pretrained_model`` 包装层，也可能直接是目录）。
_REAL_CHECKPOINT_CANDIDATES = (
    _REPO_ROOT / "models" / "act-20261117-act-wrist-smoke" / "pretrained_model",
    _REPO_ROOT / "models" / "act-20261117-act-wrist-smoke",
)
_REAL_CHECKPOINT = next(
    (candidate for candidate in _REAL_CHECKPOINT_CANDIDATES if candidate.is_dir()), None
)


def _make_checkpoint(
    root: Path,
    *,
    config: object | None = None,
    config_text: str | None = None,
    weights: bytes | None = b"fake-act-weights",
) -> Path:
    checkpoint = root / "pretrained_model"
    checkpoint.mkdir(parents=True)
    if config_text is not None:
        (checkpoint / "config.json").write_text(config_text, encoding="utf-8")
    elif config is not None:
        (checkpoint / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if weights is not None:
        (checkpoint / "model.safetensors").write_bytes(weights)
    return checkpoint


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    exit_code = main(argv)
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert isinstance(report, dict)
    return exit_code, report


def _check_ids(report: dict[str, Any]) -> set[str]:
    checks = report["checks"]
    assert isinstance(checks, list)
    return {check["id"] for check in checks}


# ---------------------------------------------------------------------------
# 合成 checkpoint：通过 / 失败码
# ---------------------------------------------------------------------------
def test_preflight_ok_with_synthetic_checkpoint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkpoint = _make_checkpoint(tmp_path, config={"type": "act"})
    exit_code, report = _run(["--checkpoint", str(checkpoint)], capsys)
    assert exit_code == 0
    assert report["ok"] is True
    assert report["errors"] == []
    assert {"checkpoint_path", "config_json", "model_type", "model_weights"} <= _check_ids(report)


def test_preflight_accepts_model_type_field(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkpoint = _make_checkpoint(tmp_path, config={"model_type": "act"})
    exit_code, report = _run(["--checkpoint", str(checkpoint)], capsys)
    assert exit_code == 0, report
    assert report["ok"] is True


def test_preflight_accepts_weights_file_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkpoint = _make_checkpoint(tmp_path, config={"type": "act"})
    exit_code, report = _run(["--checkpoint", str(checkpoint / "model.safetensors")], capsys)
    assert exit_code == 0, report


def test_preflight_missing_checkpoint_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, report = _run(["--checkpoint", str(tmp_path / "missing")], capsys)
    assert exit_code == 1
    assert report["ok"] is False
    assert report["errors"]
    assert "checkpoint_path" in _check_ids(report)


def test_preflight_broken_config_json_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkpoint = _make_checkpoint(tmp_path, config_text="{not json")
    exit_code, report = _run(["--checkpoint", str(checkpoint)], capsys)
    assert exit_code == 1
    assert report["ok"] is False


def test_preflight_non_object_config_json_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkpoint = _make_checkpoint(tmp_path, config_text="[1, 2, 3]")
    exit_code, report = _run(["--checkpoint", str(checkpoint)], capsys)
    assert exit_code == 1
    assert report["ok"] is False


def test_preflight_wrong_model_type_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkpoint = _make_checkpoint(tmp_path, config={"type": "diffusion"})
    exit_code, report = _run(["--checkpoint", str(checkpoint)], capsys)
    assert exit_code == 1
    assert report["ok"] is False
    assert any("model_type" in error for error in report["errors"])


def test_preflight_missing_weights_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkpoint = _make_checkpoint(tmp_path, config={"type": "act"}, weights=None)
    exit_code, report = _run(["--checkpoint", str(checkpoint)], capsys)
    assert exit_code == 1
    assert report["ok"] is False


def test_preflight_empty_weights_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    checkpoint = _make_checkpoint(tmp_path, config={"type": "act"}, weights=b"")
    exit_code, report = _run(["--checkpoint", str(checkpoint)], capsys)
    assert exit_code == 1
    assert report["ok"] is False


def test_preflight_rejects_wrong_weights_filename(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkpoint = _make_checkpoint(tmp_path, config={"type": "act"})
    other_file = checkpoint / "weights.bin"
    other_file.write_bytes(b"x")
    exit_code, report = _run(["--checkpoint", str(other_file)], capsys)
    assert exit_code == 1
    assert report["ok"] is False


# ---------------------------------------------------------------------------
# 可选 sha256
# ---------------------------------------------------------------------------
def test_preflight_sha256_match_and_prefix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    weights = b"fake-act-weights"
    checkpoint = _make_checkpoint(tmp_path, config={"type": "act"}, weights=weights)
    digest = hashlib.sha256(weights).hexdigest()
    exit_code, report = _run(["--checkpoint", str(checkpoint), "--sha256", digest], capsys)
    assert exit_code == 0, report
    exit_code, report = _run(
        ["--checkpoint", str(checkpoint), "--sha256", f"sha256:{digest}"], capsys
    )
    assert exit_code == 0, report


def test_preflight_sha256_mismatch_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkpoint = _make_checkpoint(tmp_path, config={"type": "act"})
    exit_code, report = _run(["--checkpoint", str(checkpoint), "--sha256", "0" * 64], capsys)
    assert exit_code == 1
    assert report["ok"] is False
    assert "sha256" in _check_ids(report)


def test_preflight_invalid_sha256_argument_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkpoint = _make_checkpoint(tmp_path, config={"type": "act"})
    with pytest.raises(SystemExit) as excinfo:
        main(["--checkpoint", str(checkpoint), "--sha256", "not-a-digest"])
    assert excinfo.value.code == 2


def test_preflight_missing_required_checkpoint_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_preflight_relative_checkpoint_uses_repo_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    _make_checkpoint(repo_root / "models" / "act-x", config={"type": "act"})
    monkeypatch.setattr(preflight, "REPO_ROOT", repo_root)
    exit_code, report = _run(["--checkpoint", "models/act-x/pretrained_model"], capsys)
    assert exit_code == 0, report


# ---------------------------------------------------------------------------
# 可选 finetune 环境检查
# ---------------------------------------------------------------------------
def _make_finetune_repo(root: Path, *, deploy_script: bool = True) -> Path:
    repo = root / "finetune"
    (repo / "deploy").mkdir(parents=True)
    (repo / "configs").mkdir(parents=True)
    if deploy_script:
        (repo / "deploy" / "run.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (repo / "configs" / "runtime_execute.yaml").write_text("schema: test\n", encoding="utf-8")
    return repo


def test_preflight_finetune_environment_checks_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _make_checkpoint(tmp_path / "ckpt", config={"type": "act"})
    repo = _make_finetune_repo(tmp_path)
    monkeypatch.setenv("SO101_VENV_PYTHON", sys.executable)
    exit_code, report = _run(
        ["--checkpoint", str(checkpoint), "--finetune-repo", str(repo)], capsys
    )
    assert exit_code == 0, report
    assert report["ok"] is True
    assert {
        "finetune_repo",
        "finetune_deploy_script",
        "finetune_venv_python",
    } <= _check_ids(report)


def test_preflight_finetune_missing_deploy_script_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _make_checkpoint(tmp_path / "ckpt", config={"type": "act"})
    repo = _make_finetune_repo(tmp_path, deploy_script=False)
    monkeypatch.setenv("SO101_VENV_PYTHON", sys.executable)
    exit_code, report = _run(
        ["--checkpoint", str(checkpoint), "--finetune-repo", str(repo)], capsys
    )
    assert exit_code == 1
    assert report["ok"] is False


def test_preflight_finetune_missing_venv_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _make_checkpoint(tmp_path / "ckpt", config={"type": "act"})
    repo = _make_finetune_repo(tmp_path)
    monkeypatch.setenv("SO101_VENV_PYTHON", str(tmp_path / "missing-python"))
    exit_code, report = _run(
        ["--checkpoint", str(checkpoint), "--finetune-repo", str(repo)], capsys
    )
    assert exit_code == 1
    assert report["ok"] is False


def test_preflight_finetune_venv_unset_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _make_checkpoint(tmp_path / "ckpt", config={"type": "act"})
    repo = _make_finetune_repo(tmp_path)
    monkeypatch.delenv("SO101_VENV_PYTHON", raising=False)
    exit_code, report = _run(
        ["--checkpoint", str(checkpoint), "--finetune-repo", str(repo)], capsys
    )
    assert exit_code == 0, report
    assert "finetune_venv_python" in _check_ids(report)


def _init_git_repo(repo: Path) -> str:
    subprocess.run(
        ["git", "-C", str(repo), "init", "-q"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def test_preflight_finetune_commit_pin_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _make_checkpoint(tmp_path / "ckpt", config={"type": "act"})
    repo = _make_finetune_repo(tmp_path)
    head = _init_git_repo(repo)
    monkeypatch.setenv("SO101_VENV_PYTHON", sys.executable)
    exit_code, report = _run(
        [
            "--checkpoint",
            str(checkpoint),
            "--finetune-repo",
            str(repo),
            "--finetune-commit",
            head[:8],
        ],
        capsys,
    )
    assert exit_code == 0, report
    assert {"finetune_git_head", "finetune_commit_pin"} <= _check_ids(report)


def test_preflight_finetune_commit_pin_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _make_checkpoint(tmp_path / "ckpt", config={"type": "act"})
    repo = _make_finetune_repo(tmp_path)
    _init_git_repo(repo)
    monkeypatch.setenv("SO101_VENV_PYTHON", sys.executable)
    exit_code, report = _run(
        [
            "--checkpoint",
            str(checkpoint),
            "--finetune-repo",
            str(repo),
            "--finetune-commit",
            "deadbeef",
        ],
        capsys,
    )
    assert exit_code == 1
    assert report["ok"] is False
    assert any("finetune_commit_pin" in error for error in report["errors"])


def test_preflight_finetune_commit_pin_requires_non_git_repo_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _make_checkpoint(tmp_path / "ckpt", config={"type": "act"})
    repo = _make_finetune_repo(tmp_path)
    monkeypatch.setenv("SO101_VENV_PYTHON", sys.executable)
    exit_code, report = _run(
        [
            "--checkpoint",
            str(checkpoint),
            "--finetune-repo",
            str(repo),
            "--finetune-commit",
            "deadbeef",
        ],
        capsys,
    )
    assert exit_code == 1
    assert report["ok"] is False


def test_preflight_commit_without_repo_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkpoint = _make_checkpoint(tmp_path, config={"type": "act"})
    with pytest.raises(SystemExit) as excinfo:
        main(["--checkpoint", str(checkpoint), "--finetune-commit", "deadbeef"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# 真实 checkpoint（Lead/DVC 放入后补跑；不存在→skip）
# ---------------------------------------------------------------------------
@pytest.mark.skipif(_REAL_CHECKPOINT is None, reason="真实 checkpoint 未放入（Lead 稍后提供）")
def test_preflight_real_checkpoint(capsys: pytest.CaptureFixture[str]) -> None:
    assert _REAL_CHECKPOINT is not None
    exit_code, report = _run(["--checkpoint", str(_REAL_CHECKPOINT)], capsys)
    assert exit_code == 0, report
    assert report["ok"] is True
    assert {"config_json", "model_type", "model_weights"} <= _check_ids(report)
