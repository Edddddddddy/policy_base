"""离线 checkpoint/finetune 环境预检 CLI（纯标准库；不 import torch/lerobot/rclpy）。

用法::

    env -u PYTHONPATH .venv/bin/python -m policy_base.policy.preflight \
        --checkpoint models/act-20261117-act-wrist-smoke \
        [--sha256 <64hex|sha256:...>] \
        [--finetune-repo /home/ubuntu/yayyrobotics/finetune --finetune-commit <pin>]

只读检查项
----------
1. ``--checkpoint`` 路径存在（目录，或 ``model.safetensors`` 文件路径；相对路径按仓库根解析）；
2. ``config.json`` 存在、可解析为 JSON object、策略类型为 ``act``（LeRobot 序列化写
   ``type`` 字段，部分导出写 ``model_type``，两者都接受）；
3. ``model.safetensors`` 存在且 ``size > 0``（不加载权重；
4. 可选 ``--sha256``：weights 全文件 sha256 与期望值一致；
5. 可选 ``--finetune-repo``：repo 目录、``deploy/run.sh``、``$SO101_VENV_PYTHON``（仅在其设置时）
   存在；可选 ``--finetune-commit``：``git rev-parse HEAD`` 与该 commit pin 前缀匹配。

输出
----
stdout 单个 JSON::

    {"ok": true, "checks": [{"id": str, "ok": bool, "detail": str}], "errors": [str]}

退出码：0 全部通过 / 1 检查失败 / 2 用法错误（argparse；含缺失 ``--checkpoint``、非法
``--sha256``/``--finetune-commit``）。本模块永不联网、不启动服务、不加载权重、不改文件。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path

from policy_base.policy.finetune import (
    FINETUNE_DEFAULT_DEPLOY_SCRIPT,
    REPO_ROOT,
    FinetuneReuseError,
    commit_matches_pin,
    read_git_head,
    resolve_checkpoint_path,
    validate_commit_pin,
)

#: checkpoint 目录内的固定文件名（与 LeRobot ``pretrained_model`` 布局一致）。
CONFIG_FILENAME = "config.json"
MODEL_WEIGHTS_FILENAME = "model.safetensors"
#: 预检要求的策略类型（ACT）。
EXPECTED_MODEL_TYPE = "act"
#: finetune venv python 的环境变量名（设置时做存在性检查，未设置则跳过）。
ENV_VENV_PYTHON = "SO101_VENV_PYTHON"
#: sha256 摘要读取分块（字节）。
_SHA256_CHUNK_BYTES = 1 << 20
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")

#: 记录单项检查结果的回调：``(check_id, ok, detail)``。
CheckRecorder = Callable[[str, bool, str], None]


def _sha256_argument(value: str) -> str:
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:") :]
    if not _SHA256_HEX.fullmatch(text):
        raise argparse.ArgumentTypeError(
            f"must be 64 hex chars (optionally prefixed with 'sha256:'), got {value!r}"
        )
    return text


def _commit_pin_argument(value: str) -> str:
    try:
        return validate_commit_pin(value)
    except FinetuneReuseError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m policy_base.policy.preflight",
        description="离线预检 ACT checkpoint 与 finetune 复用环境（只读，不加载权重/不联网）。",
    )
    parser.add_argument(
        "--checkpoint",
        required=True,
        help="LeRobot pretrained_model 目录，或 model.safetensors 文件路径（相对仓库根）",
    )
    parser.add_argument(
        "--finetune-repo",
        default=None,
        help="finetune 仓路径（给出时校验 deploy/run.sh / venv python / 可选 git HEAD）",
    )
    parser.add_argument(
        "--finetune-commit",
        default=None,
        type=_commit_pin_argument,
        help="期望的 finetune 仓 commit pin（7–40 位十六进制，需配合 --finetune-repo）",
    )
    parser.add_argument(
        "--sha256",
        default=None,
        type=_sha256_argument,
        help="期望的 model.safetensors sha256（64 hex，可带 'sha256:' 前缀）",
    )
    return parser


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_SHA256_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_checkpoint_layout(
    checkpoint: Path,
) -> tuple[Path | None, Path | None, str]:
    """把 ``--checkpoint`` 解析为 ``(config.json, model.safetensors, 错误)``。"""
    if not checkpoint.exists():
        return None, None, f"checkpoint not found: {checkpoint}"
    if checkpoint.is_dir():
        return (
            checkpoint / CONFIG_FILENAME,
            checkpoint / MODEL_WEIGHTS_FILENAME,
            "",
        )
    if checkpoint.name != MODEL_WEIGHTS_FILENAME:
        return (
            None,
            None,
            f"checkpoint file must be named {MODEL_WEIGHTS_FILENAME!r}, got {checkpoint.name!r}",
        )
    return checkpoint.parent / CONFIG_FILENAME, checkpoint, ""


def _load_checkpoint_config(config_path: Path) -> tuple[dict[str, object] | None, str]:
    """读取并解析 ``config.json``；返回 ``(mapping, 错误)``。"""
    try:
        text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return None, f"cannot read {config_path}: {error}"
    try:
        payload: object = json.loads(text)
    except ValueError as error:
        return None, f"invalid JSON in {config_path}: {error}"
    if not isinstance(payload, dict):
        return None, f"{config_path} must contain a JSON object, got {type(payload).__name__}"
    return {str(key): value for key, value in payload.items()}, ""


def _checkpoint_model_type(config: dict[str, object]) -> tuple[str | None, str]:
    """从 config.json 提取策略类型（``model_type``，LeRobot 序列化回落 ``type``）。"""
    raw = config.get("model_type", config.get("type"))
    if not isinstance(raw, str) or not raw.strip():
        return None, f"config.json has no model_type/type field (got {raw!r})"
    return raw.strip().lower(), ""


def run_preflight(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    """执行全部只读检查；返回 ``(report, exit_code)``（检查失败不抛异常）。"""
    checks: list[dict[str, object]] = []
    errors: list[str] = []

    def add(check_id: str, ok: bool, detail: str) -> None:
        checks.append({"id": check_id, "ok": bool(ok), "detail": detail})
        if not ok:
            errors.append(f"{check_id}: {detail}")

    checkpoint = resolve_checkpoint_path(args.checkpoint, repo_root=REPO_ROOT)
    if checkpoint is None:
        add("checkpoint_path", False, "--checkpoint must not be empty")
        return _build_report(checks, errors), 2

    config_path, weights_path, layout_error = _resolve_checkpoint_layout(checkpoint)
    if layout_error:
        add("checkpoint_path", False, layout_error)
        return _build_report(checks, errors), 1
    # 上面的错误分支已 return，这里两个路径必非 None（显式断言给类型检查器）。
    assert config_path is not None and weights_path is not None
    add("checkpoint_path", True, f"found: {checkpoint}")

    config_data, config_error = _load_checkpoint_config(config_path)
    if config_error:
        add("config_json", False, config_error)
    else:
        add("config_json", True, f"parsed: {config_path}")

    if config_data is not None:
        model_type, model_type_error = _checkpoint_model_type(config_data)
        if model_type == EXPECTED_MODEL_TYPE:
            add("model_type", True, f"model_type={EXPECTED_MODEL_TYPE}")
        else:
            add(
                "model_type",
                False,
                model_type_error
                or f"model_type must be {EXPECTED_MODEL_TYPE!r}, got {model_type!r}",
            )

    weights_size: int | None = None
    try:
        weights_size = weights_path.stat().st_size
    except OSError as error:
        add("model_weights", False, f"cannot stat {weights_path}: {error}")
    else:
        if weights_size > 0:
            add("model_weights", True, f"exists: {weights_path} ({weights_size} bytes)")
        else:
            add("model_weights", False, f"empty weights file: {weights_path} (size=0)")

    if args.sha256 is not None:
        try:
            actual_sha256 = _file_sha256(weights_path)
        except (OSError, ValueError) as error:
            add("sha256", False, f"cannot hash {weights_path}: {error}")
        else:
            if actual_sha256 == args.sha256:
                add("sha256", True, f"match: {actual_sha256}")
            else:
                add(
                    "sha256",
                    False,
                    f"mismatch: expected {args.sha256}, actual {actual_sha256}",
                )

    if args.finetune_repo is not None:
        _check_finetune(args, add)

    ok = not errors
    return _build_report(checks, errors), 0 if ok else 1


def _check_finetune(args: argparse.Namespace, record: CheckRecorder) -> None:
    """只读校验 finetune repo/deploy_script/venv/python 与可选 git commit pin。"""
    repo = Path(str(args.finetune_repo)).expanduser()
    if not repo.is_dir():
        record("finetune_repo", False, f"not found: {repo}")
        return
    record("finetune_repo", True, f"found: {repo}")

    deploy_script = repo / FINETUNE_DEFAULT_DEPLOY_SCRIPT
    if deploy_script.is_file():
        record("finetune_deploy_script", True, f"found: {deploy_script}")
    else:
        record("finetune_deploy_script", False, f"missing deploy script: {deploy_script}")

    venv_python = os.environ.get(ENV_VENV_PYTHON, "").strip()
    if not venv_python:
        record(
            "finetune_venv_python",
            True,
            f"skipped: {ENV_VENV_PYTHON} is not set",
        )
    else:
        venv_path = Path(venv_python).expanduser()
        if venv_path.is_file():
            record("finetune_venv_python", True, f"found: {venv_path}")
        else:
            record("finetune_venv_python", False, f"not found: {venv_path}")

    if args.finetune_commit is None:
        return
    head = read_git_head(repo)
    if head is None:
        record(
            "finetune_git_head", False, f"cannot read git HEAD (repo not a git checkout?): {repo}"
        )
        return
    record("finetune_git_head", True, f"HEAD={head}")
    if commit_matches_pin(head, args.finetune_commit):
        record(
            "finetune_commit_pin",
            True,
            f"HEAD {head} matches pin {args.finetune_commit}",
        )
    else:
        record(
            "finetune_commit_pin",
            False,
            f"HEAD {head} does not match pin {args.finetune_commit}",
        )


def _build_report(checks: list[dict[str, object]], errors: list[str]) -> dict[str, object]:
    return {"ok": not errors, "checks": checks, "errors": errors}


def main(argv: Sequence[str] | None = None) -> int:
    """预检 CLI 入口：返回进程退出码（0 通过 / 1 检查失败 / 2 用法错误）。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.finetune_commit is not None and args.finetune_repo is None:
        parser.error("--finetune-commit requires --finetune-repo")
    report, exit_code = run_preflight(args)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
