"""``policy:`` 配置段解析（纯逻辑，不 import ROS/torch/lerobot）。

实例配置 YAML 的 ``policy:`` 段由本模块解析为 :class:`PolicyConfig`。字段：

.. code-block:: yaml

   policy:
     backend: finetune          # finetune（复用 finetune 仓稳定命令面）| mock（离线测试）
     checkpoint: models/act-20261117-act-wrist-smoke/pretrained_model
                                # 相对仓库根，或 "${SO101_CHECKPOINT}" 环境引用（未设置→错误）
     allow_platform_execution: true    # 平台执行总开关（仍须 --enable-motion + --confirm-real）
     max_duration_s: 60.0
     finetune:
       repo: /home/ubuntu/yayyrobotics/finetune
       deploy_script: deploy/run.sh
       runtime_config: configs/runtime_execute.yaml
       venv_python: ${SO101_VENV_PYTHON:-/home/ubuntu/discover/so101/.venv/bin/python}
       device: cuda
       commit: null             # 可选：finetune 仓期望 commit（preflight 只读校验）
       extra_args: []
     mock:
       steps: 30
       chunk_size: 5
       step_delay_s: 0.0
       seed: 0

实现 owner：policy-dev（P3-C）。本文件由 Lead 冻结接口，实现时保持签名不变。

实现说明（不改变上述契约）：

* ``expand_env_reference`` 只展开 ``${VAR}`` / ``${VAR:-default}`` 形态的引用（可在字符串
  任意位置出现），不做 ``$VAR``/``~``/命令替换等通用 shell 展开；变量未设置或为空串时，
  有默认值用默认值，否则 :class:`PolicyConfigError`（与 finetune runtime 的严格口径一致）；
* ``checkpoint`` / ``finetune.repo`` 的相对路径按 ``base_dir``（调用方传入的仓库根）解析，
  绝对路径与 ``~`` 展开后的路径原样；本模块只做路径运算，**不检查存在性、不创建目录**；
* ``finetune`` 段在 ``backend=finetune`` 时必需（``repo`` 必填）；``backend=mock`` 时若给出
  该段也会按同一严格规则解析（便于同一配置在两种后端间切换），省略则 ``finetune=None``；
* ``finetune.commit`` 为可选 commit pin（7–40 位十六进制，前缀匹配），非法→
  :class:`PolicyConfigError`。
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from policy_base.policy.finetune import (
    FINETUNE_DEFAULT_DEPLOY_SCRIPT,
    FinetuneReuseError,
    resolve_checkpoint_path,
    validate_commit_pin,
)


class PolicyConfigError(ValueError):
    """``policy:`` 配置非法（实例入口按 exit code 2 处理）。"""


@dataclass(frozen=True)
class FinetuneSettings:
    """复用 finetune 仓的稳定命令面（``deploy/run.sh execute``）。"""

    repo: Path
    deploy_script: str = "deploy/run.sh"
    runtime_config: str = "configs/runtime_execute.yaml"
    venv_python: str = ""
    device: str = "cuda"
    commit: str | None = None
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class MockSettings:
    """离线 mock 策略参数（确定性 chunk 推理；绝不连 ROS/真机）。"""

    steps: int = 30
    chunk_size: int = 5
    step_delay_s: float = 0.0
    seed: int = 0


@dataclass(frozen=True)
class PolicyConfig:
    """解析后的 ``policy:`` 配置。"""

    backend: str
    checkpoint: str | None = None
    allow_platform_execution: bool = False
    max_duration_s: float = 60.0
    finetune: FinetuneSettings | None = None
    mock: MockSettings = field(default_factory=MockSettings)


#: 支持的策略后端（与 backend.build_round_command 的分派一致）。
BACKENDS: tuple[str, str] = ("finetune", "mock")

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_ALLOWED_FIELDS = frozenset(
    {"backend", "checkpoint", "allow_platform_execution", "max_duration_s", "finetune", "mock"}
)
_ALLOWED_FINETUNE_FIELDS = frozenset(
    {"repo", "deploy_script", "runtime_config", "venv_python", "device", "commit", "extra_args"}
)
_ALLOWED_MOCK_FIELDS = frozenset({"steps", "chunk_size", "step_delay_s", "seed"})

_DEFAULT_MAX_DURATION_S = 60.0
_DEFAULT_RUNTIME_CONFIG = "configs/runtime_execute.yaml"


def expand_env_reference(value: str, *, environ: Mapping[str, str] | None = None) -> str:
    """展开 ``"${VAR}"`` / ``"${VAR:-default}"`` 形式的引用；未设置且无默认→PolicyConfigError。

    非引用字符串原样返回（不做通用 shell 展开，避免静默吞掉拼写错误）。
    """
    environment: Mapping[str, str] = os.environ if environ is None else environ

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        actual = environment.get(name)
        if actual is None or actual == "":
            if default is not None:
                return default
            raise PolicyConfigError(
                f"unresolved environment variable ${{{name}}} in {value!r}; "
                f"set it or use ${{{name}:-default}}"
            )
        return actual

    return _ENV_REFERENCE.sub(_replace, value)


def _as_mapping(value: object, *, where: str) -> dict[str, object]:
    """严格把配置节点转成 ``{str: object}``（非 mapping/非 str key→PolicyConfigError）。"""
    if not isinstance(value, Mapping):
        raise PolicyConfigError(f"{where} must be a mapping, got {type(value).__name__}")
    mapping: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise PolicyConfigError(f"{where} keys must be strings, got {key!r}")
        mapping[key] = item
    return mapping


def _reject_unknown(mapping: Mapping[str, object], allowed: frozenset[str], *, where: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise PolicyConfigError(
            f"{where} has unknown field(s) {unknown}; allowed: {sorted(allowed)}"
        )


def _as_bool(value: object, *, where: str) -> bool:
    if not isinstance(value, bool):
        raise PolicyConfigError(f"{where} must be a boolean, got {value!r}")
    return value


def _as_int(value: object, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyConfigError(f"{where} must be an integer, got {value!r}")
    return value


def _as_float(value: object, *, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyConfigError(f"{where} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise PolicyConfigError(f"{where} must be finite, got {value!r}")
    return number


def _as_str(value: object, *, where: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise PolicyConfigError(f"{where} must be a string, got {value!r}")
    text = value.strip()
    if not text and not allow_empty:
        raise PolicyConfigError(f"{where} must not be empty")
    return text


def _parse_checkpoint(value: object, *, base_dir: Path) -> str:
    raw = _as_str(value, where="policy.checkpoint")
    expanded = expand_env_reference(raw)
    resolved = resolve_checkpoint_path(expanded, repo_root=base_dir)
    if resolved is None:  # pragma: no cover - _as_str 已拒绝空串
        raise PolicyConfigError("policy.checkpoint must not be empty")
    return str(resolved)


def _parse_finetune(value: object, *, base_dir: Path) -> FinetuneSettings:
    mapping = _as_mapping(value, where="policy.finetune")
    _reject_unknown(mapping, _ALLOWED_FINETUNE_FIELDS, where="policy.finetune")

    if "repo" not in mapping:
        raise PolicyConfigError("policy.finetune.repo is required")
    repo_text = expand_env_reference(_as_str(mapping["repo"], where="policy.finetune.repo"))
    repo = Path(repo_text).expanduser()
    if not repo.is_absolute():
        repo = Path(base_dir) / repo

    deploy_script = expand_env_reference(
        _as_str(
            mapping.get("deploy_script", FINETUNE_DEFAULT_DEPLOY_SCRIPT),
            where="policy.finetune.deploy_script",
        )
    )
    runtime_config = expand_env_reference(
        _as_str(
            mapping.get("runtime_config", _DEFAULT_RUNTIME_CONFIG),
            where="policy.finetune.runtime_config",
        )
    )
    venv_python = expand_env_reference(
        _as_str(
            mapping.get("venv_python", ""),
            where="policy.finetune.venv_python",
            allow_empty=True,
        )
    )
    device = expand_env_reference(
        _as_str(mapping.get("device", "cuda"), where="policy.finetune.device")
    )

    commit: str | None = None
    commit_raw = mapping.get("commit")
    if commit_raw is not None:
        try:
            commit = validate_commit_pin(_as_str(commit_raw, where="policy.finetune.commit"))
        except FinetuneReuseError as error:
            raise PolicyConfigError(f"policy.finetune.commit: {error}") from error

    extra_args_raw = mapping.get("extra_args", [])
    if not isinstance(extra_args_raw, (list, tuple)):
        raise PolicyConfigError(
            f"policy.finetune.extra_args must be a list, got {extra_args_raw!r}"
        )
    extra_args: list[str] = []
    for index, argument in enumerate(extra_args_raw):
        text = _as_str(argument, where=f"policy.finetune.extra_args[{index}]")
        extra_args.append(expand_env_reference(text))

    return FinetuneSettings(
        repo=repo,
        deploy_script=deploy_script,
        runtime_config=runtime_config,
        venv_python=venv_python,
        device=device,
        commit=commit,
        extra_args=tuple(extra_args),
    )


def _parse_mock(value: object) -> MockSettings:
    mapping = _as_mapping(value, where="policy.mock")
    _reject_unknown(mapping, _ALLOWED_MOCK_FIELDS, where="policy.mock")
    defaults = MockSettings()

    steps = _as_int(mapping.get("steps", defaults.steps), where="policy.mock.steps")
    if steps < 1:
        raise PolicyConfigError(f"policy.mock.steps must be >= 1, got {steps}")
    chunk_size = _as_int(
        mapping.get("chunk_size", defaults.chunk_size), where="policy.mock.chunk_size"
    )
    if chunk_size < 1:
        raise PolicyConfigError(f"policy.mock.chunk_size must be >= 1, got {chunk_size}")
    step_delay_s = _as_float(
        mapping.get("step_delay_s", defaults.step_delay_s), where="policy.mock.step_delay_s"
    )
    if step_delay_s < 0:
        raise PolicyConfigError(f"policy.mock.step_delay_s must be >= 0, got {step_delay_s}")
    seed = _as_int(mapping.get("seed", defaults.seed), where="policy.mock.seed")
    return MockSettings(steps=steps, chunk_size=chunk_size, step_delay_s=step_delay_s, seed=seed)


def load_policy_config(section: object, *, base_dir: Path) -> PolicyConfig:
    """严格解析 ``policy:`` 段；unknown 字段/非法值→:class:`PolicyConfigError`。

    * ``backend`` 必须显式给出，取值 ``finetune`` | ``mock``；
    * ``checkpoint`` 为相对路径时按 ``base_dir``（仓库根）解析；绝对路径原样；``${ENV}`` 引用先展开；
    * ``finetune`` 段仅 ``backend=finetune`` 时必需（``repo`` 必填）；``mock`` 段可省略（用默认值）；
    * 只读校验路径概不改文件；不创建任何目录。
    """
    mapping = _as_mapping(section, where="policy")
    _reject_unknown(mapping, _ALLOWED_FIELDS, where="policy")

    if "backend" not in mapping:
        raise PolicyConfigError("policy.backend is required (finetune | mock)")
    backend = _as_str(mapping["backend"], where="policy.backend")
    if backend not in BACKENDS:
        raise PolicyConfigError(f"policy.backend must be one of {list(BACKENDS)}, got {backend!r}")

    checkpoint: str | None = None
    checkpoint_raw = mapping.get("checkpoint")
    if checkpoint_raw is not None:
        checkpoint = _parse_checkpoint(checkpoint_raw, base_dir=base_dir)

    allow_platform_execution = _as_bool(
        mapping.get("allow_platform_execution", False),
        where="policy.allow_platform_execution",
    )
    max_duration_s = _as_float(
        mapping.get("max_duration_s", _DEFAULT_MAX_DURATION_S), where="policy.max_duration_s"
    )
    if max_duration_s <= 0:
        raise PolicyConfigError(f"policy.max_duration_s must be > 0, got {max_duration_s}")

    finetune_raw = mapping.get("finetune")
    finetune = None if finetune_raw is None else _parse_finetune(finetune_raw, base_dir=base_dir)
    if backend == "finetune" and finetune is None:
        raise PolicyConfigError(
            "policy.finetune section is required when policy.backend=finetune "
            "(policy.finetune.repo is required)"
        )

    mock_raw = mapping.get("mock")
    mock = MockSettings() if mock_raw is None else _parse_mock(mock_raw)

    return PolicyConfig(
        backend=backend,
        checkpoint=checkpoint,
        allow_platform_execution=allow_platform_execution,
        max_duration_s=max_duration_s,
        finetune=finetune,
        mock=mock,
    )


__all__ = [
    "BACKENDS",
    "FinetuneSettings",
    "MockSettings",
    "PolicyConfig",
    "PolicyConfigError",
    "expand_env_reference",
    "load_policy_config",
]
