# policy_base ACT 策略集成（P3-C）

本仓策略层（`src/policy_base/policy/`）负责把 ACT checkpoint 接入 instance 生命周期：
**选择后端 → 校验安全门 → 构建一轮子进程命令 → 解析轮次报告 → 离线预检**。
本层不 import torch/lerobot/rclpy，不加载权重、不连 ROS、不发运动命令；真实推理由 finetune 仓的
稳定命令面 `deploy/run.sh execute` 在子进程内执行。instance 入口只在每轮开始时调用
`build_round_command`，子进程结束后调用 `read_round_report` / `summarize_round_report`。

模块：

| 模块 | 职责 |
|---|---|
| `policy/config.py` | 严格解析实例 YAML 的 `policy:` 段为 `PolicyConfig` |
| `policy/backend.py` | 轮次命令构建（mock / finetune）、安全门、轮次报告读取/精简 |
| `policy/mock.py` | 确定性 mock chunk 推理 + `python -m` 子进程入口 |
| `policy/finetune.py` | finetune 复用常量、commit pin 校验、checkpoint 路径解析、deploy argv |
| `policy/preflight.py` | 离线 checkpoint/finetune 环境预检 CLI |

## 1. 后端选择

`policy.backend` 只允许两个值：

```yaml
policy:
  backend: finetune          # 真机执行（复用 finetune deploy/run.sh）
  checkpoint: models/act-20261117-act-wrist-smoke   # 相对仓库根；或 "${SO101_CHECKPOINT}"
  allow_platform_execution: true
  max_duration_s: 60.0
  finetune:
    repo: /home/ubuntu/yayyrobotics/finetune
    deploy_script: deploy/run.sh
    runtime_config: configs/runtime_execute.yaml
    venv_python: "${SO101_VENV_PYTHON:-/home/ubuntu/discover/so101/.venv/bin/python}"
    device: cuda
    commit: null             # 可选 commit pin（preflight 只读比对）
    extra_args: []
```

```yaml
policy:
  backend: mock              # 离线：不连 ROS/推理服务，不需要 checkpoint/GPU
  allow_platform_execution: false
  mock:
    steps: 30
    chunk_size: 5
    step_delay_s: 0.0
    seed: 0
```

解析规则（`load_policy_config(section, base_dir=<仓库根>)`）：

- unknown 字段/非法类型一律 `PolicyConfigError`（instance 入口按 exit code 2 处理）；
- `checkpoint` 支持 `${VAR}` / `${VAR:-default}` 环境引用，未设置且无默认→`PolicyConfigError`；
  相对路径按 `base_dir` 解析，绝对路径原样（不检查存在性——存在性由 preflight/运行时校验）；
- `finetune` 段在 `backend=finetune` 时必需且 `repo` 必填；`mock` 段可省略（用默认值）；
- `finetune.commit` 为可选 commit pin（7–40 位十六进制）。

见 `tests/unit/test_policy_config.py`。

## 2. 安全门（不可绕过）

`backend.build_round_command(policy, report_path=..., enable_motion=..., confirm_real=..., dry_run=...)`：

| 场景 | 行为 |
|---|---|
| `dry_run=True` | **强制 mock 后端**，即使配置是 finetune，也绝不构建真机命令 |
| `backend=mock` | 构建 mock 子进程命令（不需要 checkpoint/GPU/ROS） |
| `backend=finetune` | 必须**同时**满足三道门，否则 `PolicyBackendError`（消息列出缺哪道门） |

finetune 三道门：

1. 配置 `policy.allow_platform_execution: true`（仓库/实例级总开关，默认 false）；
2. 调用方显式 `--enable-motion`（操作员断言：允许发布真实运动命令）；
3. 调用方显式 `--confirm-real`（确认真机窗口 + 用户授权）。

另：`checkpoint is None`、`policy.finetune` 缺失、`max_duration_s` 截断后不在 1..300 秒
（`deploy/run.sh execute` 的强制区间）都会在构建阶段直接拒绝。租约（lease）由
factory_manager 持有，本仓/实例绝不 acquire/release。

## 3. mock / 离线用法

mock 是确定性纯函数：输出只取决于 `(steps, chunk_size, seed)`（自实现 64 位 LCG，无全局随机
状态），每个 chunk 生成 `chunk_size` 个 6 维动作，逐关节应用安全夹紧（步长 `0.08 rad`、URDF
限位），chunk 之间以上一 chunk 末动作为锚点保证连续。它只**模拟发布**，不 import rclpy、不发
`/arm/joint_command`。

```bash
# 直接跑一轮 mock 并写轮次报告（不需要 checkpoint/venv/GPU）
env -u PYTHONPATH .venv/bin/python -m policy_base.policy.mock \
  --report /tmp/round_report.json --steps 10 --chunk-size 3 --step-delay-s 0 --seed 1 \
  [--trace /tmp/trace.jsonl]
```

报告 schema `so101.policy_base.round_report.v1`：

```json
{"schema": "so101.policy_base.round_report.v1", "status": "ok", "backend": "mock",
 "checkpoint": null, "steps": 10, "published": 10, "duration_s": 0.0,
 "chunks": 4, "error": null}
```

`published` = 实际模拟发布的命令数（= `steps`，必须 > 0）；`chunks = ceil(steps/chunk_size)`。
`--trace` 每行 `{"chunk_index", "step_index", "action"}`。退出码：0 成功 / 1 写报告失败 /
2 参数非法。instance 的 `--dry-run` 与 `backend: mock` 都走这条路径。

## 4. finetune 复用命令与版本 pin

`backend=finetune` 构建的 argv（不执行，只交给 instance runner 启动子进程）：

```bash
bash <finetune.repo>/deploy/run.sh execute \
  --config <finetune.runtime_config> \
  --enable-motion --confirm-real \
  --max-duration-s <int(policy.max_duration_s)> [finetune.extra_args...]
```

env 注入（runner 与继承环境合并）：`SO101_CHECKPOINT=<checkpoint>`（必填）、
`SO101_VENV_PYTHON=<venv_python>`、`SO101_DEVICE=<device>`（空值不注入）；`cwd=<finetune.repo>`。
复用的 runtime 配置 schema 为 `so101.act.runtime_config.v1`；本站安全阈值（步长 0.08 rad、
限速 0.3 rad/s、新鲜度 0.2 s、时长上限等）仍由 finetune runner 受信侧执行，本仓不放松。

版本/commit pin：`policy.finetune.commit` 显式声明期望的 finetune 仓 commit（7–40 位十六进制，
按前缀匹配 `git rev-parse HEAD`）；未设置 = 不 pin。本仓只读校验，不 fetch/checkout/修改
finetune 仓。常量与 helper 见 `policy/finetune.py`。

## 5. 预检（离线，只读）

```bash
env -u PYTHONPATH .venv/bin/python -m policy_base.policy.preflight \
  --checkpoint models/act-20261117-act-wrist-smoke \
  [--sha256 <64hex|sha256:...>] \
  [--finetune-repo /home/ubuntu/yayyrobotics/finetune --finetune-commit <pin>]
```

检查项：checkpoint 路径存在（目录，或 `model.safetensors` 文件路径，相对路径按仓库根解析）、
`config.json` 可解析为 JSON object 且策略类型为 `act`（LeRobot 写 `type`，部分导出写
`model_type`，两者都接受）、`model.safetensors` 存在且 size > 0、可选 sha256 比对；给出
`--finetune-repo` 时校验 repo 目录 / `deploy/run.sh` / `$SO101_VENV_PYTHON`（仅在其设置时）
存在性，给出 `--finetune-commit` 时比对 git HEAD。

输出 stdout 单个 JSON：`{"ok": bool, "checks": [{"id","ok","detail"}], "errors": [...]}`。
退出码：0 全通过 / 1 检查失败 / 2 用法错误（含缺失 `--checkpoint`、非法 sha256/commit pin）。
本 CLI 不加载权重、不联网、不启动服务。

> 当前仓库模型目录为 DVC 拉取后的 `models/act-20261117-act-wrist-smoke/`（`config.json` /
> `model.safetensors` 直接位于该目录）；`policy.checkpoint` 必须指向实际目录，不要再拼
> `pretrained_model/` 层，除非 DVC 产物确实带该包装层。

## 6. 轮次报告与 instance 事件字段

`read_round_report(path)`：报告缺失/非 JSON/非 mapping → `None`（不抛，调用方降级用 returncode
判定）。`summarize_round_report(report)` 固定输出
`{"backend", "steps", "published", "duration_s", "status", "checkpoint"}`，缺失/非法字段用
`None`/`0` 兜底，保证 JSON 可序列化。finetune 子进程不写本仓报告，`read_round_report` 返回
`None` 属预期。

## 7. 验证

```bash
cd /home/ubuntu/yayyrobotics/policy_base
env -u PYTHONPATH .venv/bin/python -m pytest tests/unit/test_policy_*.py -q
.venv/bin/mypy src/policy_base/policy
.venv/bin/ruff check src/policy_base/policy tests/unit/test_policy_*.py
.venv/bin/ruff format --check src/policy_base/policy tests/unit/test_policy_*.py
```

覆盖：dry-run 强制 mock、finetune 三道门逐项拒绝、`${ENV}` 未设置报错、mock 进程级 E2E
（子进程写报告 `published > 0`）、确定性/安全夹紧、preflight 合成 checkpoint 通过与损坏/缺文件失败码、
真实 checkpoint（`models/act-20261117-act-wrist-smoke`，存在时）预检通过。

## 8. 未验证边界（离线通过 ≠ 真机通过）

- **真机执行未做**：本层只构建 `deploy/run.sh execute` 命令与安全门，从未在真机上启动过；
  真机动作需用户授权窗口内由 Lead 按 finetune runbook 执行并现场确认 `/arm/joint_states`、
  相机、lease 与安全门。
- 真实 GPU/venv 上的 ACT 推理、推理服务连通性、RTT 未在本层验证（属 finetune 组件与真机窗口）。
- mock 报告只是模拟发布计数，不能作为真机运动证据。
- DVC 权重分发/`dvc pull` 由 IM/交付流程负责；本仓只消费已就位 checkpoint 并做只读预检。
- 未联调 ROS 话题/namespace/instance 协议（instance 层负责；本层无 ROS 依赖）。
