# policy_base

SO-101 的 **IM（inference_manager）实例化 ACT 策略仓库**，是 `rule_base` 之外的第二种推理执行交付物：
EP 下发任务 → FM → IM 拉起本仓库实例 → 站端 runner 调用 ACT 策略 → 驱动 `/arm/joint_command`（真机执行）。

> 规划与验收：`../.scratch/session-15-platform-diag/PLAN-e2e-closed-loop.md`（P3 部分）、
> `../.scratch/session-15-platform-diag/PLAN-P3.md`（本轮执行计划）
> 编码约定：[AGENTS.md](AGENTS.md)

## 边界

- **只做**：FM/IM instance 协议（stopped/reset/hold/entry）+ ACT 策略加载/推理集成（先离线/mock）
  + DVC 权重分发。
- **不做**：训练（finetune）、数据采集/质检（so101-station/EP）、录制/评测（recorder/env/benchmark）、平台业务。
- **不改变平台控制面语义**：租约（lease）由 factory_manager 持有，实例绝不 acquire/release。
- **安全**：真机执行必须用户在场 + EP 一次性授权；`--enable-motion`/`--confirm-real` 双门不可绕过；
  不放松安全阈值；离线/mock 通过不代表真机通过。

## 目录

```text
src/policy_base/instance/   # entry/protocol/keepalive/runner/config（对齐 rule_base instance 冻结契约）
src/policy_base/policy/     # ACT 策略集成：config/backend/mock/finetune/preflight
configs/pick_place/         # live_ros2_env.yaml（finetune 后端）/ mock_ros2_env.yaml（离线 mock）
models/                     # DVC 权重（git 只跟踪 .dvc 指针；见 docs/dvc-distribution.md）
submission.yaml             # IM 交付声明（policy-based + GPU + uv argv）
docs/                       # 接口/部署/IM 集成/DVC 说明
tests/unit  tests/smoke     # 默认离线单测/冒烟
```

## 快速开始（离线）

```bash
cd /home/ubuntu/yayyrobotics/policy_base
uv sync --locked                 # Python 3.12 + 开发依赖（.venv/）
env -u PYTHONPATH .venv/bin/python -m pytest tests/unit tests/smoke -q
.venv/bin/mypy src/policy_base
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests

# 离线 chunk 推理（mock 策略子进程；不连 ROS/GPU/真机）
.venv/bin/python -m policy_base.policy.mock --report /tmp/pb_round.json --steps 10 --chunk-size 3 --seed 1
cat /tmp/pb_round.json

# checkpoint/DVC 预检（DVC 权重，198 MB；见 docs/dvc-distribution.md）
dvc pull
.venv/bin/python -m policy_base.policy.preflight --checkpoint models/act-20261117-act-wrist-smoke
```

实例入口（FM/IM 拉起时使用；需要已 source 的 ROS 2 Jazzy）：

```bash
# 本地 mock 实例（仅测试；--dry-run 强制 mock 后端，禁止真机）
python -m policy_base.instance.entry --config configs/pick_place/mock_ros2_env.yaml --dry-run

# 真机执行（仅在用户授权窗口；双门缺一拒绝；默认配置仍要求 allow_platform_execution）
python -m policy_base.instance.entry --config configs/pick_place/live_ros2_env.yaml \
    --enable-motion --confirm-real
```

## 权重分发（DVC）

git 只提交 `.dvc` 指针与 DVC 配置；权重在 MinIO `s3://so101-archive/dvc/policy_base`（endpoint
`http://127.0.0.1:9000`），`dvc pull` 按内容哈希还原。**禁止提交权重/凭据**。详见
[docs/dvc-distribution.md](docs/dvc-distribution.md)。

## IM 集成（git 源 + DVC）

本轮 P3-4 spike 结论：IM 用 **路线 A**（`runtime.repo_source: git`，repo_url 可为
`file:///home/ubuntu/yayyrobotics/policy_base` 或绝对路径），保留既有 `dvc pull`；无需改 IM 生产代码。
路线 B（local 模式副本内 dvc pull）因副本无 `.git` 被否决。配置/证据/回滚见
[docs/im-integration.md](docs/im-integration.md)。

## 文档

- [instance-mode.md](docs/instance-mode.md) - instance 协议、CLI、配置字段、安全门、与 rule_base 契约差异；
- [policy-integration.md](docs/policy-integration.md) - ACT 策略后端、mock/离线推理、finetune 复用、预检；
- [dvc-distribution.md](docs/dvc-distribution.md) - DVC 权重分发/验证/凭据；
- [im-integration.md](docs/im-integration.md) - IM 拉起路线（git 源 + DVC）与平台接线状态。

## 状态（2026-11-17，P3 开发中）

- ✅ 离线/mock：instance 协议、策略后端构建、mock chunk 推理、checkpoint 预检、DVC 权重分发、IM 侧 prepare（git 源 + DVC）；
- ⏳ 未验证：真机单轮执行（arm 硬件 deactivate + 用户授权窗口未开）、EP/A1/FM 平台接线（待用户确认）、
  IM 实际 launch（domain 0）与 E2E 录制/归档；
- 回滚：IM 配置 `repo_source` 改回 `local` 即回退到本地模式；FM pin 保持 rule_base
  `6401700f98e61664686fa422893f928a8258af6c`（切换前须用户确认，一键回滚见平台配置注释）。
