"""确定性 mock 策略（离线 chunk 推理 + 轮次子进程入口；无 ROS/torch/checkpoint 依赖）。

用途
----
* 单测/离线 E2E：不连 ROS 2、不连推理服务、不加载 checkpoint、不 import torch；
* 实例 ``dry-run``（``build_round_command(dry_run=True)``）与 ``backend: mock`` 的轮次执行；
* 由 :mod:`policy_base.policy.backend` 以子进程调用::

    python -m policy_base.policy.mock --report <json> --steps N --chunk-size M \
        --step-delay-s S --seed K [--trace <jsonl>]

确定性口径
----------
动作只取决于 ``(steps, chunk_size, seed)``：自实现 64 位 LCG（不依赖全局随机状态，避免不同
解释器/线程影响），每个 chunk 用 ``(seed, chunk_index)`` 派生独立初始状态；chunk 之间以
「上一 chunk 的最后一个动作」为锚点，保证边界连续。每个动作：

1. 相对上一条命令的逐关节增量 ≤ :data:`MAX_JOINT_STEP_RAD`（0.08 rad，安全夹紧常量）；
2. 逐关节 clamp 到 :data:`JOINT_MIN_RAD` / :data:`JOINT_MAX_RAD`；
3. 6 维且全部 finite。

这些关节名/限位/步长常量与 finetune ``configs/runtime_execute.yaml`` 的 safety 段一致，
**不得放松**。本模块只做「模拟发布」：绝不 import rclpy、绝不发布 /arm/joint_command、
绝不打开串口或连接真机。

CLI 报告
--------
``--report`` 写 JSON（schema :data:`policy_base.policy.backend.ROUND_REPORT_SCHEMA`）：
``status=ok``、``backend=mock``、``published=steps``（> 0）、``chunks=ceil(steps/chunk_size)``、
``checkpoint=None``、``error=None``。``--trace`` 另写 JSONL（每行一个已模拟发布动作）。
退出码：0 成功 / 1 写报告失败 / 2 参数非法（argparse）。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from policy_base.policy.backend import ROUND_REPORT_SCHEMA

#: 动作维度 / 关节数（SO-101 6 自由度）。
ACTION_DIM = 6
JOINT_NAMES: tuple[str, ...] = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
)
#: 与 finetune runtime_execute.yaml safety 段一致的关节限位（rad；不得放松）。
JOINT_MIN_RAD: tuple[float, ...] = (-1.91986, -1.6964, -1.69, -1.65806, -2.74385, 0.0)
JOINT_MAX_RAD: tuple[float, ...] = (1.91986, 1.74533, 1.5323, 1.65806, 2.84121, 1.74533)
#: 相对上一条命令的逐关节最大单步增量（rad；安全夹紧常量，不得放松）。
MAX_JOINT_STEP_RAD = 0.08
#: mock 轨迹相对锚点的随机幅度（rad；远小于单步上限）。
AMPLITUDE_RAD = 0.02
#: 无观测时的安全锚点：各关节限位中点。
DEFAULT_JOINT_ANCHOR_RAD: tuple[float, ...] = tuple(
    0.5 * (low + high) for low, high in zip(JOINT_MIN_RAD, JOINT_MAX_RAD, strict=True)
)

DEFAULT_STEPS = 30
DEFAULT_CHUNK_SIZE = 5
DEFAULT_STEP_DELAY_S = 0.0
DEFAULT_SEED = 0

_MASK64 = (1 << 64) - 1
_LCG_MULTIPLIER = 6364136223846793005
_LCG_INCREMENT = 1442695040888963407
_GOLDEN_GAMMA = 0x9E3779B97F4A7C15


@dataclass(frozen=True)
class MockRoundPlan:
    """一次 mock 轮次的确定性执行计划（纯数据，可复现）。"""

    steps: int
    chunk_size: int
    seed: int
    #: 生成的 chunk（每个 chunk 恰好 chunk_size 个 6 维动作；末尾 chunk 可能只有部分被发布）。
    chunks: tuple[tuple[tuple[float, ...], ...], ...]

    @property
    def chunk_count(self) -> int:
        """生成的 chunk 数（``ceil(steps / chunk_size)``）。"""
        return len(self.chunks)

    def iter_published(self) -> Iterator[tuple[int, int, tuple[float, ...]]]:
        """按 chunk/step 顺序产出实际模拟发布动作：``(chunk_index, 全局 step_index, action)``。"""
        step_index = 0
        for chunk_index, chunk in enumerate(self.chunks):
            for action in chunk:
                if step_index >= self.steps:
                    return
                yield chunk_index, step_index, action
                step_index += 1

    def published_actions(self) -> tuple[tuple[float, ...], ...]:
        """实际模拟发布动作序列（长度 = ``steps``，契约要求 > 0）。"""
        return tuple(action for _chunk, _step, action in self.iter_published())

    def published_action_count(self) -> int:
        """实际模拟发布命令数（= ``steps``）。"""
        return sum(1 for _entry in self.iter_published())


def _lcg_next(state: int) -> int:
    """64 位 LCG 单步（Knuth MMIX 常数；确定性、无全局状态）。"""
    return (state * _LCG_MULTIPLIER + _LCG_INCREMENT) & _MASK64


def _derive_state(seed: int, chunk_index: int) -> int:
    """由 ``(seed, chunk_index)`` 派生 chunk 初始状态（彼此独立、可复现）。"""
    state = (seed & _MASK64) ^ (((chunk_index + 1) * _GOLDEN_GAMMA) & _MASK64)
    for _ in range(4):
        state = _lcg_next(state)
    return state


def _unit_interval(state: int) -> tuple[float, int]:
    """推进 LCG 并返回 ``([0, 1)``, 新状态)``。"""
    state = _lcg_next(state)
    return (state >> 11) / float(1 << 53), state


def _normalize_anchor(previous_action: Sequence[float] | None) -> list[float]:
    """校验/夹紧上一动作作为锚点；``None`` → 限位中点。"""
    if previous_action is None:
        return list(DEFAULT_JOINT_ANCHOR_RAD)
    if len(previous_action) != ACTION_DIM:
        raise ValueError(
            f"previous_action must have {ACTION_DIM} values, got {len(previous_action)}"
        )
    anchor: list[float] = []
    for index, raw in enumerate(previous_action):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError(f"previous_action[{index}] is not finite: {raw!r}")
        anchor.append(min(JOINT_MAX_RAD[index], max(JOINT_MIN_RAD[index], value)))
    return anchor


def generate_action_chunk(
    previous_action: Sequence[float] | None,
    *,
    chunk_size: int,
    seed: int,
    chunk_index: int,
) -> list[list[float]]:
    """纯函数：生成一个确定性 chunk（``(chunk_size, 6)``，finite、限位内、步长夹紧）。"""
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size!r}")
    if chunk_index < 0:
        raise ValueError(f"chunk_index must be >= 0, got {chunk_index!r}")
    current = _normalize_anchor(previous_action)
    state = _derive_state(seed, chunk_index)
    chunk: list[list[float]] = []
    for _ in range(chunk_size):
        action: list[float] = []
        for joint in range(ACTION_DIM):
            unit, state = _unit_interval(state)
            target = current[joint] + (unit * 2.0 - 1.0) * AMPLITUDE_RAD
            delta = target - current[joint]
            delta = min(MAX_JOINT_STEP_RAD, max(-MAX_JOINT_STEP_RAD, delta))
            value = current[joint] + delta
            value = min(JOINT_MAX_RAD[joint], max(JOINT_MIN_RAD[joint], value))
            if not math.isfinite(value):  # pragma: no cover - 锚点/增量均有限，防御性
                raise ValueError(f"non-finite mock action at joint {joint}")
            action.append(float(value))
        chunk.append(action)
        current = action
    return chunk


def plan_mock_round(*, steps: int, chunk_size: int, seed: int) -> MockRoundPlan:
    """纯函数：规划一轮（确定性；chunk 数 = ``ceil(steps / chunk_size)``）。"""
    if steps < 1:
        raise ValueError(f"steps must be >= 1, got {steps!r}")
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size!r}")
    chunk_count = (steps + chunk_size - 1) // chunk_size
    chunks: list[tuple[tuple[float, ...], ...]] = []
    previous_action: tuple[float, ...] | None = None
    for chunk_index in range(chunk_count):
        raw_chunk = generate_action_chunk(
            previous_action, chunk_size=chunk_size, seed=seed, chunk_index=chunk_index
        )
        chunk = tuple(tuple(action) for action in raw_chunk)
        chunks.append(chunk)
        previous_action = chunk[-1]
    return MockRoundPlan(steps=steps, chunk_size=chunk_size, seed=seed, chunks=tuple(chunks))


def build_round_report(plan: MockRoundPlan, *, duration_s: float) -> dict[str, object]:
    """生成轮次报告（schema :data:`ROUND_REPORT_SCHEMA`；JSON 可序列化）。"""
    return {
        "schema": ROUND_REPORT_SCHEMA,
        "status": "ok",
        "backend": "mock",
        "checkpoint": None,
        "steps": plan.steps,
        "published": plan.published_action_count(),
        "duration_s": max(0.0, float(duration_s)),
        "chunks": plan.chunk_count,
        "error": None,
    }


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_trace(path: Path, plan: MockRoundPlan) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {"chunk_index": chunk_index, "step_index": step_index, "action": list(action)},
            ensure_ascii=False,
            sort_keys=True,
        )
        for chunk_index, step_index, action in plan.iter_published()
    ]
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"must be an integer, got {value!r}") from error
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {number}")
    return number


def _non_negative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"must be a number, got {value!r}") from error
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError(f"must be finite and >= 0, got {value!r}")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m policy_base.policy.mock",
        description="确定性 mock 策略：本地生成 6 维 chunk 动作并模拟发布（不连 ROS/真机）。",
    )
    parser.add_argument("--report", required=True, type=Path, help="轮次报告 JSON 输出路径")
    parser.add_argument(
        "--steps", type=_positive_int, default=DEFAULT_STEPS, help="模拟发布的命令数（>= 1）"
    )
    parser.add_argument(
        "--chunk-size",
        type=_positive_int,
        default=DEFAULT_CHUNK_SIZE,
        help="每个 chunk 的动作数（>= 1）",
    )
    parser.add_argument(
        "--step-delay-s",
        type=_non_negative_float,
        default=DEFAULT_STEP_DELAY_S,
        help="每条模拟发布命令的等待秒数（>= 0）",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="确定性随机种子")
    parser.add_argument("--trace", type=Path, default=None, help="可选的 JSONL 动作轨迹输出路径")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """mock 轮次子进程入口：返回进程退出码（0 成功 / 1 写报告失败 / 2 参数非法）。"""
    args = _build_parser().parse_args(argv)
    plan = plan_mock_round(steps=args.steps, chunk_size=args.chunk_size, seed=args.seed)
    started_at = time.monotonic()
    if args.step_delay_s > 0.0:
        for _chunk_index, _step_index, _action in plan.iter_published():
            time.sleep(args.step_delay_s)
    duration_s = max(0.0, time.monotonic() - started_at)
    report = build_round_report(plan, duration_s=duration_s)
    try:
        _write_json(args.report, report)
        if args.trace is not None:
            _write_trace(args.trace, plan)
    except OSError as error:
        print(f"mock policy: cannot write report/trace: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    raise SystemExit(main())
