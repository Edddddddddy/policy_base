# AI 编码约定（policy_base）

适用于本仓库；与用户当前明确要求冲突时以用户要求为准。
本文件参考（结构/口径）：
`C:\Users\lcy\code\discover\discoverrobotics\s2-restricted\data-factory\station_peripheral\rule_base\AGENTS.md`，
并按 policy_base = “IM 可拉起的第二种实例仓库”的边界调整。

## 修改原则

- 修改前阅读相关代码、测试和 `git diff`，保留用户已有改动；以当前实现为准，不沿用旧对话假设。
- 解决根因，只改与任务直接相关的代码；必要时同步更新调用、配置、测试和文档。
- 优先原地修改、复用现有实现、删除冗余；不增加推测性配置、兼容层或单次使用的通用框架。
- 不顺手重构、重命名、搬移文件、升级依赖或全仓格式化；不擅自暂存、提交或回退改动。
- 不通过放宽容差、延长超时、固定等待或关闭校验掩盖故障；安全阈值（关节步长/限位/新鲜度/超时）不得放松。
- 不伪造成功：环境判定/评测失败时如实记录，不写“成功”证据。

## Python 规范

- 通用风格遵循 [PEP 8](https://peps.python.org/pep-0008/)，docstring 遵循 [PEP 257](https://peps.python.org/pep-0257/)，
  类型注解参考 [Python typing 规范](https://typing.python.org/en/latest/spec/)；不在此重复社区规范。
- 项目配置优先：Python 3.12；按 `pyproject.toml` 使用 Ruff（行宽 100）与严格 mypy；新增/修改的生产函数补全类型，
  不降低检查要求。
- 依赖用 `uv sync --locked` 安装；虚拟环境与缓存保持在 `.venv/`、`.uv-cache/`。

## 项目边界（本仓库职责）

- **定位**：inference_manager（IM）可拉起的第二种算法实例仓库（第一种是 `rule_base`）。复用 `rule_base` 的
  instance 协议骨架（`stopped` 2 Hz latched / `env/reset_status` 上升沿 / 每轮 stopped=False→执行→True /
  SIGTERM 清理），算法模块替换为 ACT 策略。
- **不改变**平台控制面语义：租约（lease）由 factory_manager 持有，实例**绝不** acquire/release；
  不修改 arm_app/env_app/recorder/benchmark/EP 的业务；实例失败返回明确退出码并记录 stage/error。
- **算法**：加载 ACT checkpoint（policy server + 站端 runner，优先复用 finetune 的 `so101_act_server` /
  `so101_act_runtime` 能力，版本/commit 显式声明）；不在此仓库做训练、数据质检、录制、评测。
- **权重分发**：DVC（remote = MinIO S3 兼容存储）；仓库只提交 `.dvc` 指针与 `dvc.lock`，
  **禁止提交** `.safetensors`/checkpoints/模型权重；凭据不入库（走环境变量/本机配置）。
- **安全**：真机运动仅经 EP 任务一次性授权 + 用户在场窗口；`--enable-motion` 与 `--confirm-real` 双门不可绕过；
  单轮时长有上限；空闲 hold 只发实测关节；配置里显式写 `allow_platform_execution` 语义与使用条件。
- **接口/契约**：instance topic/namespace/prefix 由 `INFERENCE_NAMESPACE`/`DOMAIN_ID` 等环境变量注入；
  与 `rule_base` 的契约保持兼容；新增行为写单测（无 ROS 可测的纯逻辑优先）。

## 目录约定（目标态）

```text
src/policy_base/instance/     # entry/protocol/keepalive（对齐 rule_base instance 契约）
src/policy_base/policy/       # ACT 策略加载/推理/runner 集成
configs/pick_place/live_ros2_env.yaml
submission.yaml               # IM 交付声明（policy-based + resources + uv argv）
uv.lock
tests/unit  tests/smoke
docs/                          # 接口/部署/排障
```

## 验证与交付

从仓库根目录运行（依赖缺失先 `uv sync --locked`）：

```bash
env -u PYTHONPATH .venv/bin/python -m pytest tests/unit tests/smoke
.venv/bin/mypy src/policy_base
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
```

- 行为修改先跑相关测试，再跑上述检查；只补与行为有关的回归测试。纯文档修改检查 `git diff --check` 即可。
- **默认离线验证**（mock instance 协议/单测）；真机执行只在用户明确授权的窗口进行，不重复索要会话内授权；
  真实动作前必须确认 `/arm/joint_states`、相机、lease 状态与安全门。
- DVC 验证：全新克隆后 `dvc pull` 能取到 checkpoint，再跑 preflight/离线 chunk 推理。
- 交付简述：修改内容、检查结果、未验证范围（离线通过不代表真机通过）、回滚方式。
