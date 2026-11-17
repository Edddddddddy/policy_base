"""Purpose: keep the policy_base instance repository contract (offline, no ROS/hardware).

Checks the module boundaries the IM relies on: no ROS/torch/rule_base imports in the
offline instance modules, no lease management calls, declared configs/safety semantics,
no model weights tracked by git, and the documented instance-mode CLI/config contract.

Usage: env -u PYTHONPATH .venv/bin/python -m pytest tests/smoke/test_repository_contract.py -q
"""

from __future__ import annotations

import ast
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTANCE_DIR = ROOT / "src/policy_base/instance"
DOCS = ROOT / "docs/instance-mode.md"
MOCK_CONFIG = ROOT / "configs/pick_place/mock_ros2_env.yaml"
LIVE_CONFIG = ROOT / "configs/pick_place/live_ros2_env.yaml"

OFFLINE_MODULES = ("protocol.py", "keepalive.py", "config.py", "runner.py")
FORBIDDEN_ROOTS = {"rclpy", "sensor_msgs", "std_msgs", "torch", "lerobot", "rule_base"}


def _module_level_import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in tree.body:  # module-level statements only (function imports are allowed)
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("name", (*OFFLINE_MODULES, "entry.py"))
def test_module_level_imports_stay_offline(name):
    roots = _module_level_import_roots(INSTANCE_DIR / name)
    assert not (roots & FORBIDDEN_ROOTS), (
        f"{name} imports {roots & FORBIDDEN_ROOTS} at module level"
    )


def _all_import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("name", OFFLINE_MODULES)
def test_offline_modules_import_no_ros_or_ml_packages(name):
    forbidden = _all_import_roots(INSTANCE_DIR / name) & FORBIDDEN_ROOTS
    assert not forbidden, f"{name} must stay importable without ROS/ML: imports {forbidden}"


def test_entry_wires_ros_only_inside_the_serve_path():
    source = (INSTANCE_DIR / "entry.py").read_text(encoding="utf-8")
    assert "import rclpy" in source  # deferred import inside _serve
    tree = ast.parse(source)
    module_imports = {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }
    assert "rclpy" not in module_imports


def test_instance_code_never_manages_the_lease_or_arm_authority():
    forbidden = (
        "AcquireAuthority",
        "ReleaseAuthority",
        "acquire_authority",
        "release_authority",
        "forward_controller/commands",
    )
    for path in INSTANCE_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, (
                f"{path.name} must not touch the lease/arm authority: {needle}"
            )


def test_pyproject_has_no_ros_or_ml_runtime_dependency():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = " ".join(data["project"]["dependencies"]).lower()
    for forbidden in ("rclpy", "torch", "lerobot", "rule_base"):
        assert forbidden not in dependencies
    assert data["project"]["scripts"]["so101-policy-base"] == "policy_base.instance.entry:main"


def test_instance_configs_declare_the_frozen_schema_and_safe_defaults():
    live = yaml.safe_load(LIVE_CONFIG.read_text(encoding="utf-8"))
    mock = yaml.safe_load(MOCK_CONFIG.read_text(encoding="utf-8"))
    for document in (live, mock):
        assert document["schema"] == "so101.policy_base.instance_config.v1"
        assert document["instance"]["hold_hz"] == 4.0
        assert document["instance"]["stopped_hz"] == 2.0
        assert document["ros2"]["prefix"] == "/arm"
        assert len(document["ros2"]["joint_names"]) == 6

    assert live["policy"]["backend"] == "finetune"
    assert live["policy"]["allow_platform_execution"] is True
    assert live["policy"]["checkpoint"] == "models/act-20261117-act-wrist-smoke"
    assert live["policy"]["finetune"]["device"] == "cuda"
    assert "${SO101_VENV_PYTHON:-" in live["policy"]["finetune"]["venv_python"]
    assert len(live["policy"]["finetune"]["commit"]) == 40

    assert mock["policy"]["backend"] == "mock"
    assert mock["policy"]["allow_platform_execution"] is False
    assert "checkpoint" not in mock["policy"]
    assert "禁止真机" in MOCK_CONFIG.read_text(encoding="utf-8")


def test_instance_mode_documentation_is_present_and_complete():
    text = DOCS.read_text(encoding="utf-8")
    for needle in (
        "--enable-motion",
        "--confirm-real",
        "--dry-run",
        "mock_ros2_env.yaml",
        "live_ros2_env.yaml",
        "allow_platform_execution",
        "inference/stopped",
        "env/reset_status",
        "round_report",
        "rule_base",
        "未验证",
    ):
        assert needle in text, f"docs/instance-mode.md is missing {needle!r}"


def test_no_model_weights_are_tracked_by_git():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    tracked = [line for line in result.stdout.splitlines() if line.strip()]
    weights = [
        line
        for line in tracked
        if line.endswith((".safetensors", ".pt", ".pth", ".ckpt", ".bin"))
        or (line.startswith("models/") and not line.endswith(".dvc"))
    ]
    assert weights == [], f"model weights/checkpoints must stay in DVC, tracked: {weights}"
