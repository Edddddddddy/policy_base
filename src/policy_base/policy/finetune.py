"""finetune 仓复用辅助：版本/commit pin 常量、checkpoint 路径解析、deploy 命令构建。

本模块纯标准库（不 import torch/lerobot/rclpy），也不执行真机命令：只提供常量与纯函数，
供 :mod:`policy_base.policy.backend` 构建 ``deploy/run.sh execute`` 命令、供
:mod:`policy_base.policy.preflight` 做只读环境校验。

finetune 仓的稳定命令面（``deploy/run.sh``；参数契约见 finetune ``docs/dev-contract.md`` §8）：

.. code-block:: bash

   bash deploy/run.sh execute --config configs/runtime_execute.yaml \
     --enable-motion --confirm-real --max-duration-s N

版本/commit pin 语义
--------------------
``policy.finetune.commit`` 显式声明期望的 finetune 仓 commit（7–40 位十六进制，按前缀匹配
``git rev-parse HEAD``）；未设置 = 不 pin（preflight 只记录 HEAD，不做比对）。本模块只**只读**
校验，不 fetch/checkout finetune 仓，也不在运行时修改它。

安全边界：``--enable-motion`` / ``--confirm-real`` 双门与单轮时长上限由
:mod:`policy_base.policy.backend` 在构建命令前强制校验；``--max-duration-s`` 必须落在
``deploy/run.sh`` 强制的 :data:`FINETUNE_EXECUTE_MIN_DURATION_S` ..
:data:`FINETUNE_EXECUTE_MAX_DURATION_S` 秒区间内。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查使用，避免 config <-> finetune 运行期循环导入
    from policy_base.policy.config import FinetuneSettings

#: 本复用契约的 schema id（写入文档/预检输出，便于版本对账）。
FINETUNE_REUSE_SCHEMA = "so101.policy_base.finetune_reuse.v1"
#: finetune ``deploy/run.sh`` 的真机运动子命令（唯一允许被本仓拼装的子命令）。
FINETUNE_DEPLOY_SUBCOMMAND = "execute"
#: finetune ``deploy/run.sh execute`` 复用的 runtime 配置 schema（只读对账用）。
FINETUNE_RUNTIME_CONFIG_SCHEMA = "so101.act.runtime_config.v1"
#: ``deploy/run.sh execute --max-duration-s`` 的合法区间（秒；run.sh 强制 1..300）。
FINETUNE_EXECUTE_MIN_DURATION_S = 1
FINETUNE_EXECUTE_MAX_DURATION_S = 300
#: finetune 仓默认 deploy 脚本（相对 repo 根）。
FINETUNE_DEFAULT_DEPLOY_SCRIPT = "deploy/run.sh"
#: commit pin 允许长度（短 sha 7 位到完整 40 位）。
COMMIT_PIN_MIN_LENGTH = 7
COMMIT_PIN_MAX_LENGTH = 40
#: ``git rev-parse HEAD`` 的只读超时（秒）；超时视为不可用（不阻塞入口）。
GIT_HEAD_TIMEOUT_S = 5.0

_COMMIT_PIN_PATTERN = re.compile(
    r"[0-9a-fA-F]{%d,%d}" % (COMMIT_PIN_MIN_LENGTH, COMMIT_PIN_MAX_LENGTH)
)
_FULL_GIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
#: 仓库根（``<repo>/src/policy_base/policy/finetune.py`` → parents[3]，editable 安装场景）。
REPO_ROOT = Path(__file__).resolve().parents[3]


class FinetuneReuseError(ValueError):
    """finetune 复用参数非法（commit pin / 时长 / 路径形态）。"""


def validate_commit_pin(pin: str) -> str:
    """校验并规范化 commit pin（7–40 位十六进制，忽略大小写/首尾空白）。"""
    if not isinstance(pin, str):
        raise FinetuneReuseError(f"commit pin must be a string, got {type(pin).__name__}")
    text = pin.strip().lower()
    if not _COMMIT_PIN_PATTERN.fullmatch(text):
        raise FinetuneReuseError(
            f"commit pin must be {COMMIT_PIN_MIN_LENGTH}-{COMMIT_PIN_MAX_LENGTH} hex chars, "
            f"got {pin!r}"
        )
    return text


def commit_matches_pin(head: str, pin: str) -> bool:
    """``git rev-parse HEAD`` 是否匹配 commit pin（前缀匹配，短 sha 合法）。"""
    if not isinstance(head, str):
        raise FinetuneReuseError(f"git head must be a string, got {type(head).__name__}")
    return head.strip().lower().startswith(validate_commit_pin(pin))


def resolve_checkpoint_path(checkpoint: str | Path | None, *, repo_root: str | Path) -> Path | None:
    """解析 checkpoint 路径：绝对路径原样（``~`` 展开），相对路径按 ``repo_root`` 拼接。

    ``None`` / 空字符串 → ``None``。本函数只做路径运算，不检查存在性、不创建目录。
    """
    if checkpoint is None:
        return None
    text = str(checkpoint).strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path(repo_root) / path
    return path


def read_git_head(repo: str | Path, *, timeout_s: float = GIT_HEAD_TIMEOUT_S) -> str | None:
    """只读读取 ``git -C <repo> rev-parse HEAD``；不可用/超时/非 git 仓 → ``None``。"""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=float(timeout_s),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    head = completed.stdout.strip().lower()
    if not _FULL_GIT_SHA_PATTERN.fullmatch(head):
        return None
    return head


def validate_execute_duration_s(max_duration_s: float) -> int:
    """校验单轮时长并返回整数秒（``deploy/run.sh execute`` 要求 1..300 的整数秒）。"""
    if isinstance(max_duration_s, bool) or not isinstance(max_duration_s, (int, float)):
        raise FinetuneReuseError(
            f"max_duration_s must be a number, got {type(max_duration_s).__name__}"
        )
    seconds = float(max_duration_s)
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        raise FinetuneReuseError(f"max_duration_s must be finite, got {max_duration_s!r}")
    whole = int(seconds)
    if whole < FINETUNE_EXECUTE_MIN_DURATION_S or whole > FINETUNE_EXECUTE_MAX_DURATION_S:
        raise FinetuneReuseError(
            f"max_duration_s must truncate to {FINETUNE_EXECUTE_MIN_DURATION_S}.."
            f"{FINETUNE_EXECUTE_MAX_DURATION_S} integer seconds, got {max_duration_s!r}"
        )
    return whole


def build_deploy_execute_argv(
    finetune: FinetuneSettings,
    *,
    max_duration_s: float,
    extra_args: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """构建 finetune ``deploy/run.sh execute`` 的 argv（不启动进程，不执行真机）。"""
    seconds = validate_execute_duration_s(max_duration_s)
    deploy_script = Path(finetune.repo) / finetune.deploy_script
    argv = [
        "bash",
        str(deploy_script),
        FINETUNE_DEPLOY_SUBCOMMAND,
        "--config",
        finetune.runtime_config,
        "--enable-motion",
        "--confirm-real",
        "--max-duration-s",
        str(seconds),
    ]
    argv.extend(str(argument) for argument in extra_args)
    return tuple(argv)


__all__ = [
    "COMMIT_PIN_MAX_LENGTH",
    "COMMIT_PIN_MIN_LENGTH",
    "FINETUNE_DEFAULT_DEPLOY_SCRIPT",
    "FINETUNE_DEPLOY_SUBCOMMAND",
    "FINETUNE_EXECUTE_MAX_DURATION_S",
    "FINETUNE_EXECUTE_MIN_DURATION_S",
    "FINETUNE_REUSE_SCHEMA",
    "FINETUNE_RUNTIME_CONFIG_SCHEMA",
    "GIT_HEAD_TIMEOUT_S",
    "REPO_ROOT",
    "FinetuneReuseError",
    "build_deploy_execute_argv",
    "commit_matches_pin",
    "read_git_head",
    "resolve_checkpoint_path",
    "validate_commit_pin",
    "validate_execute_duration_s",
]
