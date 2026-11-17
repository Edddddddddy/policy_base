"""轮次命令构建与轮次报告解析（instance 入口的唯一 policy 调用面；纯逻辑、无 ROS）。

冻结接口（Lead 2026-11-17）：

* :func:`build_round_command` 由 :mod:`policy_base.instance.entry` 在开始一轮时调用，
  返回一个可被 :mod:`policy_base.instance.runner` 以子进程启动的命令；
* 安全门：``dry_run=True`` 一律强制 mock 后端（离线测试，绝不真机）；finetune 后端必须同时满足
  ``policy.allow_platform_execution and enable_motion and confirm_real``，否则
  :class:`PolicyBackendError`，任何调用方都不能绕过；
* 轮次报告：mock 子进程写 ``--report`` JSON（schema ``so101.policy_base.round_report.v1``），
  :func:`read_round_report` 读取并容忍缺失/损坏（返回 None，不抛），
  :func:`summarize_round_report` 生成入口事件用的精简字段。

实现 owner：policy-dev（P3-C）。本文件由 Lead 冻结接口，实现时保持签名不变。

实现说明（不改变上述契约）：

* mock 命令固定 ``sys.executable -m policy_base.policy.mock``（不依赖实例进程 cwd 里的脚本
  路径；报告由 ``--report`` 指定）；``description="mock"`` 便于入口事件区分后端；
* finetune 命令的 argv 由 :func:`policy_base.policy.finetune.build_deploy_execute_argv` 构建
  （``bash <repo>/deploy/run.sh execute --enable-motion --confirm-real --max-duration-s N``），
  env 只注入非空的 ``SO101_CHECKPOINT`` / ``SO101_VENV_PYTHON`` / ``SO101_DEVICE``，由 runner
  与继承环境合并；本模块只构建命令，**绝不启动进程、绝不发运动命令**。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from policy_base.policy.config import PolicyConfig
from policy_base.policy.finetune import (
    FinetuneReuseError,
    build_deploy_execute_argv,
)

ROUND_REPORT_SCHEMA = "so101.policy_base.round_report.v1"


class PolicyBackendError(RuntimeError):
    """无法构建一轮策略命令（配置/安全门/依赖缺失；入口按 exit code 1 处理）。"""


@dataclass(frozen=True)
class RoundCommand:
    """一轮策略子进程的冻结启动契约。"""

    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    report_path: Path | None = None
    description: str = ""


def _build_mock_command(policy: PolicyConfig, *, report_path: Path) -> RoundCommand:
    mock = policy.mock
    argv = (
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
    return RoundCommand(
        argv=argv,
        env={},
        cwd=None,
        report_path=report_path,
        description="mock",
    )


def build_round_command(
    policy: PolicyConfig,
    *,
    report_path: Path,
    enable_motion: bool = False,
    confirm_real: bool = False,
    dry_run: bool = False,
) -> RoundCommand:
    """构建一轮策略命令（mock 子进程或 finetune ``deploy/run.sh execute``）。"""
    if dry_run or policy.backend == "mock":
        return _build_mock_command(policy, report_path=report_path)

    if policy.backend != "finetune":
        raise PolicyBackendError(
            f"unsupported policy backend {policy.backend!r} (expected 'finetune' or 'mock')"
        )

    missing_gates: list[str] = []
    if not policy.allow_platform_execution:
        missing_gates.append("policy.allow_platform_execution=true")
    if not enable_motion:
        missing_gates.append("--enable-motion")
    if not confirm_real:
        missing_gates.append("--confirm-real")
    if missing_gates:
        raise PolicyBackendError(
            "refusing to build finetune real-robot command; missing safety gate(s): "
            + ", ".join(missing_gates)
            + " (all of policy.allow_platform_execution, --enable-motion, --confirm-real "
            "are required)"
        )

    if policy.checkpoint is None:
        raise PolicyBackendError(
            "finetune backend requires policy.checkpoint (got None; set the checkpoint path "
            "or ${SO101_CHECKPOINT})"
        )
    finetune = policy.finetune
    if finetune is None:
        raise PolicyBackendError(
            "finetune backend requires policy.finetune settings (repo/deploy_script/runtime_config)"
        )

    try:
        argv = build_deploy_execute_argv(
            finetune,
            max_duration_s=policy.max_duration_s,
            extra_args=finetune.extra_args,
        )
    except FinetuneReuseError as error:
        raise PolicyBackendError(f"cannot build finetune deploy command: {error}") from error

    env: dict[str, str] = {"SO101_CHECKPOINT": policy.checkpoint}
    if finetune.venv_python:
        env["SO101_VENV_PYTHON"] = finetune.venv_python
    if finetune.device:
        env["SO101_DEVICE"] = finetune.device

    return RoundCommand(
        argv=argv,
        env=env,
        cwd=finetune.repo,
        report_path=report_path,
        description="finetune",
    )


def read_round_report(path: Path | None) -> dict[str, object] | None:
    """读取子进程轮次报告；缺失/非 JSON/非 mapping→``None``（调用方降级为 returncode 判定）。"""
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        payload: object = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return {str(key): value for key, value in payload.items()}


def _summarize_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _summarize_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _summarize_duration(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return 0.0
    return number


def summarize_round_report(report: Mapping[str, object] | None) -> dict[str, object]:
    """把轮次报告精简为实例事件字段（backend/steps/published/duration_s/status/checkpoint）。"""
    if report is None:
        return {
            "backend": None,
            "steps": 0,
            "published": 0,
            "duration_s": 0.0,
            "status": None,
            "checkpoint": None,
        }
    return {
        "backend": _summarize_text(report.get("backend")),
        "steps": _summarize_count(report.get("steps")),
        "published": _summarize_count(report.get("published")),
        "duration_s": _summarize_duration(report.get("duration_s")),
        "status": _summarize_text(report.get("status")),
        "checkpoint": _summarize_text(report.get("checkpoint")),
    }


__all__ = [
    "ROUND_REPORT_SCHEMA",
    "PolicyBackendError",
    "RoundCommand",
    "build_round_command",
    "read_round_report",
    "summarize_round_report",
]
