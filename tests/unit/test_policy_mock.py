"""确定性 mock 策略单测（含进程级 E2E；离线，不连 ROS/真机）。"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from policy_base.policy.backend import ROUND_REPORT_SCHEMA, read_round_report
from policy_base.policy.mock import (
    ACTION_DIM,
    DEFAULT_JOINT_ANCHOR_RAD,
    JOINT_MAX_RAD,
    JOINT_MIN_RAD,
    MAX_JOINT_STEP_RAD,
    build_round_report,
    generate_action_chunk,
    main,
    plan_mock_round,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 纯函数 / 确定性 / 安全夹紧
# ---------------------------------------------------------------------------
def test_plan_is_deterministic() -> None:
    first = plan_mock_round(steps=10, chunk_size=3, seed=1)
    second = plan_mock_round(steps=10, chunk_size=3, seed=1)
    assert first == second
    assert first.published_actions() == second.published_actions()


def test_different_seed_changes_actions() -> None:
    first = plan_mock_round(steps=5, chunk_size=5, seed=0)
    second = plan_mock_round(steps=5, chunk_size=5, seed=1)
    assert first.published_actions() != second.published_actions()


def test_plan_shapes_and_safety_limits() -> None:
    plan = plan_mock_round(steps=10, chunk_size=3, seed=1)
    assert plan.chunk_count == 4  # ceil(10 / 3)
    assert plan.published_action_count() == 10
    assert len(plan.chunks) == 4
    for chunk in plan.chunks:
        assert len(chunk) == 3
        for action in chunk:
            assert len(action) == ACTION_DIM
    previous: tuple[float, ...] | None = None
    for action in plan.published_actions():
        for index, value in enumerate(action):
            assert math.isfinite(value)
            assert JOINT_MIN_RAD[index] <= value <= JOINT_MAX_RAD[index]
            if previous is not None:
                assert abs(value - previous[index]) <= MAX_JOINT_STEP_RAD + 1e-12
        previous = action


def test_generate_action_chunk_starts_from_limit_midpoint() -> None:
    chunk = generate_action_chunk(None, chunk_size=1, seed=0, chunk_index=0)
    assert len(chunk) == 1
    for index, value in enumerate(chunk[0]):
        assert abs(value - DEFAULT_JOINT_ANCHOR_RAD[index]) <= 0.02 + 1e-12


def test_generate_action_chunk_is_deterministic() -> None:
    first = generate_action_chunk(None, chunk_size=4, seed=7, chunk_index=2)
    second = generate_action_chunk(None, chunk_size=4, seed=7, chunk_index=2)
    assert first == second


def test_generate_action_chunk_validates_inputs() -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        generate_action_chunk(None, chunk_size=0, seed=0, chunk_index=0)
    with pytest.raises(ValueError, match="chunk_index"):
        generate_action_chunk(None, chunk_size=1, seed=0, chunk_index=-1)
    with pytest.raises(ValueError, match="previous_action"):
        generate_action_chunk([0.0], chunk_size=1, seed=0, chunk_index=0)


def test_plan_mock_round_validates_inputs() -> None:
    with pytest.raises(ValueError, match="steps"):
        plan_mock_round(steps=0, chunk_size=1, seed=0)
    with pytest.raises(ValueError, match="chunk_size"):
        plan_mock_round(steps=1, chunk_size=0, seed=0)


def test_build_round_report_contract() -> None:
    plan = plan_mock_round(steps=10, chunk_size=3, seed=1)
    report = build_round_report(plan, duration_s=0.5)
    assert report == {
        "schema": ROUND_REPORT_SCHEMA,
        "status": "ok",
        "backend": "mock",
        "checkpoint": None,
        "steps": 10,
        "published": 10,
        "duration_s": 0.5,
        "chunks": 4,
        "error": None,
    }
    assert json.dumps(report)


# ---------------------------------------------------------------------------
# CLI（in-process）
# ---------------------------------------------------------------------------
def test_main_writes_report(tmp_path: Path) -> None:
    report_path = tmp_path / "nested" / "round_report.json"
    exit_code = main(
        [
            "--report",
            str(report_path),
            "--steps",
            "10",
            "--chunk-size",
            "3",
            "--step-delay-s",
            "0",
            "--seed",
            "1",
        ]
    )
    assert exit_code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema"] == ROUND_REPORT_SCHEMA
    assert report["status"] == "ok"
    assert report["backend"] == "mock"
    assert report["checkpoint"] is None
    assert report["steps"] == 10
    assert report["published"] == 10
    assert report["chunks"] == 4
    assert report["error"] is None
    assert report["duration_s"] >= 0.0
    assert set(report) == {
        "schema",
        "status",
        "backend",
        "checkpoint",
        "steps",
        "published",
        "duration_s",
        "chunks",
        "error",
    }


def test_main_writes_trace(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    trace_path = tmp_path / "trace.jsonl"
    exit_code = main(
        [
            "--report",
            str(report_path),
            "--steps",
            "5",
            "--chunk-size",
            "2",
            "--step-delay-s",
            "0",
            "--seed",
            "3",
            "--trace",
            str(trace_path),
        ]
    )
    assert exit_code == 0
    lines = trace_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    entries = [json.loads(line) for line in lines]
    assert [entry["chunk_index"] for entry in entries] == [0, 0, 1, 1, 2]
    assert all(len(entry["action"]) == ACTION_DIM for entry in entries)
    assert entries[0]["step_index"] == 0 and entries[-1]["step_index"] == 4


def test_main_honors_step_delay(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    exit_code = main(
        [
            "--report",
            str(report_path),
            "--steps",
            "2",
            "--chunk-size",
            "1",
            "--step-delay-s",
            "0.01",
            "--seed",
            "0",
        ]
    )
    assert exit_code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["duration_s"] >= 0.01


@pytest.mark.parametrize(
    "extra_args",
    [["--steps", "0"], ["--chunk-size", "0"], ["--step-delay-s", "-1"]],
)
def test_main_rejects_invalid_arguments(tmp_path: Path, extra_args: list[str]) -> None:
    report_path = tmp_path / "report.json"
    with pytest.raises(SystemExit) as excinfo:
        main(["--report", str(report_path), *extra_args])
    assert excinfo.value.code == 2
    assert not report_path.exists()


# ---------------------------------------------------------------------------
# 进程级 E2E：`python -m policy_base.policy.mock ...` → 报告 JSON
# ---------------------------------------------------------------------------
def test_mock_module_process_e2e(tmp_path: Path) -> None:
    report_path = tmp_path / "process_report.json"
    trace_path = tmp_path / "process_trace.jsonl"
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "policy_base.policy.mock",
            "--report",
            str(report_path),
            "--steps",
            "10",
            "--chunk-size",
            "3",
            "--step-delay-s",
            "0",
            "--seed",
            "1",
            "--trace",
            str(trace_path),
        ],
        cwd=_REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = read_round_report(report_path)
    assert report is not None
    assert report["schema"] == ROUND_REPORT_SCHEMA
    assert report["status"] == "ok"
    assert report["backend"] == "mock"
    assert report["steps"] == 10
    assert report["published"] > 0
    assert report["published"] == 10
    assert report["checkpoint"] is None
    assert report["error"] is None
    # 跨进程确定性：--trace 动作与同 seed 的纯函数计划逐值一致。
    plan = plan_mock_round(steps=10, chunk_size=3, seed=1)
    traced = [
        tuple(json.loads(line)["action"])
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert traced == [tuple(action) for action in plan.published_actions()]
