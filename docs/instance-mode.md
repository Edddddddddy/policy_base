# policy_base 实例模式（IM 可拉起的 ACT 策略实例）

> 契约来源：rule_base 实例模式的冻结契约（`inference/stopped` 2 Hz latched、
> `env/reset_status` 严格 False→True 上升沿、每轮 stopped=False→执行→True、SIGTERM 清理、
> 退出码 0/1/2）。policy_base 复用同一套 wire 语义，只把"一轮做什么"从 rule_base 的
> Pick 业务替换为 **ACT 策略子进程**（`policy_base.policy.backend.RoundCommand`）。
>
> 证据分档：**mock / 本机离线**。本仓库实例模式**未做真机验证**（见 §14）；真机执行必须由
> 用户在场窗口 + EP 一次性授权触发，`--enable-motion` 与 `--confirm-real` 双门不可绕过。

## 1 定位与边界

policy_base 是 inference_manager（IM）拉起并管理的**第二种算法实例仓库**（第一种是 rule_base）：

```text
inference_manager ──launch──▶ python -m policy_base.instance.entry --config <yaml> [--dry-run]
                                │  控制面：<ns>/inference/stopped(2Hz,TL) / <ns>/env/reset_status
                                │  hold：  <prefix>/joint_command（空闲/轮间 4Hz，实测关节）
                                └─每轮──▶ policy_base.policy.backend.build_round_command(...)
                                          └─ 子进程：mock 或 finetune deploy/run.sh execute
```

- **做**：实例协议（entry/protocol/keepalive/runner/config）+ 每轮策略子进程的启动契约与
  报告读取（report schema `so101.policy_base.round_report.v1`）。
- **不做**：训练（finetune 仓）、数据采集/质检/录制/评测（so101-station/recorder/env/benchmark）、
  平台业务。本仓库不 import torch/lerobot/rclpy 到离线模块，也不发真机运动命令。
- **租约**：由 factory_manager 持有。实例**绝不** acquire/release；只发 `<prefix>/joint_command`
  hold 心跳让 arm_app 续租。

## 2 模块结构

| 文件 | 职责 | ROS 依赖 |
|---|---|---|
| `instance/protocol.py` | env 解析、namespace→topic、`reset_status` 边沿状态机、退出码归类、JSON 事件行 | 无（可离线单测） |
| `instance/keepalive.py` | hold 命令构造（实测关节位置/夹爪保持）、2–5 Hz 频率校验、start/stop | 无 |
| `instance/config.py` | 实例 YAML 严格解析（schema/instance/ros2），`policy:` 段交给 `policy.config` | 无 |
| `instance/runner.py` | 单轮子进程：`RoundCommand` 启动、env 合并、单轮超时、SIGTERM→宽限→SIGKILL、读报告 | 无（子进程不导入） |
| `instance/entry.py` | rclpy 入口：env、warmup、stopped 发布、reset 订阅、hold、轮次循环、信号清理 | 有（`_serve()` 内延迟 import） |

`policy/` 包（policy-dev 负责）提供 `config/backend/mock/finetune/preflight`；
`instance/` 只通过冻结接口 `build_round_command` / `read_round_report` /
`summarize_round_report` 调用，不 import rule_base。

## 3 入口 CLI

```bash
# 真机/生产（IM 按 submission.yaml 拉起；本轮只保证 mock/离线证据）
python -m policy_base.instance.entry --config configs/pick_place/live_ros2_env.yaml \
    --enable-motion --confirm-real

# mock / 离线（无 ROS、无 GPU、无 checkpoint；禁止真机）
python -m policy_base.instance.entry --config configs/pick_place/mock_ros2_env.yaml --dry-run
```

| 参数 | 默认 | 语义 |
|---|---|---|
| `--config PATH` | 必填 | 实例 YAML（schema `so101.policy_base.instance_config.v1`） |
| `--dry-run` | false | **强制 mock 后端**（即使 config `backend: finetune`），仅测试用；绝不真机 |
| `--enable-motion` | false | 真机运动闸门之一（另需 `--confirm-real` 与 `policy.allow_platform_execution: true`） |
| `--confirm-real` | false | 真机人工确认闸门；入口不提供任何绕过路径 |
| `--warmup-timeout S` | 配置 `instance.warmup_timeout_s`（30） | 等首个 `<prefix>/joint_states` 样本上限；超时 exit 2 |
| `--round-timeout S` | 配置 `instance.round_timeout_s`（600） | 单轮策略子进程超时；超时 SIGTERM→宽限→SIGKILL，实例 exit 1 |
| `--hold-hz HZ` | 配置 `instance.hold_hz`（4） | 空闲 hold 频率，CLI 校验必须在 2–5 Hz |
| `--stop-grace S` | 5 | 关停子进程的 SIGTERM→SIGKILL 宽限 |

CLI 越界值由 argparse 直接拒绝（exit 2）；配置文件错误由入口映射 exit 2。

## 4 实例配置字段

```yaml
schema: so101.policy_base.instance_config.v1   # 必须精确匹配，否则 exit 2

instance:
  namespace_env: INFERENCE_NAMESPACE  # 读命名空间的环境变量名（契约默认值，可改用于测试 harness）
  warmup_timeout_s: 30.0              # >0
  round_timeout_s: 600.0              # >0
  hold_hz: 4.0                        # 2–5 Hz（实例契约）
  stopped_hz: 2.0                     # (0, 10] Hz；契约标准值 2 Hz

ros2:
  prefix: /arm                        # 绝对 ROS 名（无空段/空白）
  joint_names: [shoulder_pan, shoulder_lift, elbow_flex, wrist_flex, wrist_roll, gripper]
                                      # 恰好 6 个、互不重复、非空
  # 派生：joint_states_topic = "<prefix>/joint_states"
  #       joint_command_topic = "<prefix>/joint_command"

policy:                               # 交给 policy_base.policy.config.load_policy_config
  backend: finetune | mock
  checkpoint: models/... | ${ENV}
  allow_platform_execution: true|false
  max_duration_s: 60.0
  finetune: {repo, deploy_script, runtime_config, venv_python, device, commit, extra_args}
  mock: {steps, chunk_size, step_delay_s, seed}
```

- 加载是**严格**的：unknown 字段/类型错误/取值越界 → `InstanceConfigError`（`ValueError`）→ exit 2；
  只读，不创建目录、不改文件。
- `checkpoint` 相对路径按**仓库根**解析（`base_dir`）；`${VAR}` / `${VAR:-default}` 引用先展开
  （未设置且无默认 → 错误，不静默）。
- 声明配置：`configs/pick_place/live_ros2_env.yaml`（真机 finetune，checkpoint 为 DVC 产物
  `models/act-20261117-act-wrist-smoke`，finetune commit 显式 pin）与
  `configs/pick_place/mock_ros2_env.yaml`（mock，`allow_platform_execution: false`）。

## 5 环境变量（实例契约，与 rule_base 相同）

| 变量 | 必填 | 行为 |
|---|---|---|
| `INFERENCE_NAMESPACE`（或 `instance.namespace_env` 指定的变量） | 否 | 空串=无前缀；非空必须绝对名（`/` 开头、无空段）；缺失仅 warning |
| `DOMAIN_ID` | **是** | 整数、非负；进程未继承 `ROS_DOMAIN_ID` 时由它导出；已继承且不一致 → exit 2 |
| `STATION_TYPE` | **是** | 本期必须 `pick_place`，否则 exit 2 |
| `CLIENT_ID` / `LEASE_ID` | 否 | 缺失/空 → 结构化 WARN，不失败；实例只记录，不 acquire/release |
| `INFERENCE_ID` | 否 | 仅日志（IM 注入）；非法值 WARN 后忽略 |

## 6 话题与 QoS

`namespace = INFERENCE_NAMESPACE`；空串即下表绝对名，非空加 `<ns>` 前缀（无双重斜杠）：

| 话题 | 类型/方向 | QoS |
|---|---|---|
| `<ns>/inference/stopped` | `std_msgs/Bool` 发布（2 Hz 电平，初始 True） | RELIABLE + TRANSIENT_LOCAL + KEEP_LAST depth 1 |
| `<ns>/env/reset_status` | `std_msgs/Bool` 订阅（电平） | RELIABLE，depth 1 |
| `<prefix>/joint_states` | `sensor_msgs/JointState` 订阅（warmup + hold 实测源） | RELIABLE，depth 10 |
| `<prefix>/joint_command` | `sensor_msgs/JointState` 发布（hold 心跳，6 关节） | RELIABLE，depth 10 |

机械臂话题**不走** `INFERENCE_NAMESPACE`（`<prefix>` 来自配置，默认 `/arm`）。
`stopped=True` = 本轮未开始/已结束；`False` = 正在执行一轮策略。

## 7 状态机（冻结语义）

```text
启动 → 读 config/env → rclpy.init → 建话题（stopped 初始 True）
    → warmup：等首个 <prefix>/joint_states（默认 30s；超时 exit 2；期间 SIGTERM → exit 0）
    → instance_ready → gate.mark_ready() → stopped=True（2 Hz 持续 latched）
    → 空闲：等 <ns>/env/reset_status 的 False→True 上升沿（hold 4Hz 持续）
    → 边沿：keepalive.stop() → stopped=False → build_round_command → runner.run(on_tick=spin)
    → 读报告 → 成功：stopped=True → keepalive.start() → 回空闲
    → 运行期 reset 消息丢弃（不排队、不回放）
    → 单轮失败/超时/spawn 错误：instance_failed(stage/error/报告字段) → stopped=True →
      drain ≥1 个发布周期 → exit 1（不自动重试）
    → SIGTERM/SIGINT：打断子进程（SIGTERM→5s 宽限→SIGKILL）→ 停定时器 → rclpy.shutdown → exit 0
```

边沿语义（与 rule_base 一致，解决 level-latch 重复触发）：

1. 启动后必须先观测至少一次 `False` 才武装；
2. 武装状态下第一个 `True` = 一次新复位完成 → 消费边沿、开轮、解除武装；
3. 轮次结束后需重新等待 `False`；持续 `True` 电平不得触发新轮；
4. 运行期间收到的 reset 丢弃。

每轮 `stopped` 严格按 `True(latched) → False → True` 翻转，轮内不再重复开轮。

## 8 keepalive（租约保持）

- 空闲期（warmup 完成后至轮次开始前、以及轮次之间）以 `instance.hold_hz`（默认 4 Hz，限 2–5 Hz）
  发布 `<prefix>/joint_command`：6 关节名/顺序来自 `ros2.joint_names`，位置 = 最近一次实测
  `<prefix>/joint_states` 位置（**夹爪也保持**，不改变目标姿态）。
- 无实测样本 / 样本不完整 / 非有限值 → 跳过该周期（不发畸形命令）。
- **策略子进程运行期间严格停发**（避免与策略子进程的动作流量抢写），轮间恢复。
- arm_app 约 5 s 无流量会掉租约；4 Hz 留 >4 s 余量。

## 9 单轮 runner 与轮次报告

| 项 | 值 |
|---|---|
| 启动契约 | `policy_base.policy.backend.RoundCommand(argv, env, cwd, report_path)` |
| mock 后端 | `sys.executable -m policy_base.policy.mock --report <abs> --steps ...`（`--dry-run` 强制走这里） |
| finetune 后端 | `bash <repo>/deploy/run.sh execute --config <runtime_config> --enable-motion --confirm-real --max-duration-s N`，env 注入 `SO101_CHECKPOINT/SO101_VENV_PYTHON/SO101_DEVICE`，cwd=finetune 仓库 |
| 子进程 env | 继承实例进程 env，再用 `RoundCommand.env` 覆盖（`SO101_*` 优先） |
| cwd | `RoundCommand.cwd`，未指定时=policy_base 仓库根 |
| 报告 | 子进程结束后 `backend.read_round_report(report_path)`；缺失/损坏 → `None`（不抛），退出码仍权威 |
| 报告 schema | `so101.policy_base.round_report.v1`：`status/backend/checkpoint/steps/published/duration_s/chunks/error` |
| 超时 | 默认 600 s；超时 → SIGTERM → `--stop-grace`（5 s）→ SIGKILL → 实例 exit 1 |
| 关停 | SIGTERM → 宽限 → SIGKILL；`run_child` 的 `finally` 保证异常/超时都不泄漏子进程 |

入口事件（`round_finished` / `instance_failed`）带
`status/returncode/backend/steps/published/duration_s/checkpoint`（报告缺失时字段为默认值，
`status` 回退为子进程结果状态）。

## 10 退出码与结构化事件

| 退出码 | 含义 |
|---|---|
| `0` | SIGTERM/SIGINT 正常停止；或轮次被 SIGTERM 打断后正常退出 |
| `1` | 轮次失败：子进程非 0 / 超时 / spawn 错误 / `build_round_command` 安全门或配置错误 / runner 异常 |
| `2` | 配置/env/warmup/ROS 依赖（rclpy 不可用）失败 |

stdout 为单行 JSON（`{"event": ...}`，`ensure_ascii=False`）；关键事件：

| `event` | 关键字段 | 时机 |
|---|---|---|
| `instance_warning` | warning | 可选 env 缺失/非法 |
| `instance_starting` | namespace/domain_id/station_type/client_id/lease_id/inference_id/dry_run/enable_motion/confirm_real/backend/config/时间参数 | env 解析通过后 |
| `instance_topics` | stopped/reset_status/joint_states/joint_command/prefix/stopped_hz/hold_hz | 建话题时 |
| `instance_ready` | namespace/inference_id/topics/hold_hz | warmup 完成 |
| `round_started` | round/inference_id/dry_run/enable_motion/confirm_real | 每轮开始（stopped=False 后） |
| `round_finished` | round/rounds_completed + 报告字段 | 轮次成功 |
| `round_interrupted` | round/signal/returncode + 报告字段 | SIGTERM 打断本轮（exit 0） |
| `instance_failed` | **stage/error**（stage ∈ config/environment/warmup/ros2_init/backend/spawn/round/timeout/runtime）+ inference_id/round/returncode/报告字段 | 失败退出 |
| `instance_stopping` | signal/rounds_completed/rounds_failed/discarded_resets | 正常停止 |

## 11 安全门（不可放宽）

- **真机运动三门**：`policy.allow_platform_execution: true` + `--enable-motion` + `--confirm-real`。
  由 `policy_base.policy.backend.build_round_command` 校验；缺任一 → `PolicyBackendError` →
  实例 `instance_failed(stage=backend)` → exit 1。入口**不**提供绕过/降级路径。
- **`--dry-run` 强制 mock**：即使配置 `backend: finetune`，`--dry-run` 也只构建 mock 命令；
  仅测试用，禁止真机。
- **租约**：实例不 acquire/release（源码无 `AcquireAuthority`/`ReleaseAuthority`），
  hold 只发实测关节位置。
- **单轮上限**：`policy.max_duration_s`（默认 60 s）+ `instance.round_timeout_s`（默认 600 s）双重约束。
- **hold 只在空闲/轮间**：策略子进程运行时停发，避免双写。
- mock 配置 `allow_platform_execution: false`：即使误加 `--enable-motion/--confirm-real`，
  finetune 路径也不可达（`backend: mock`）。

## 12 mock / 离线用法

```bash
cd /home/ubuntu/yayyrobotics/policy_base

# 1) 实例入口（无 ROS 时预期 exit 2 + instance_failed(stage=ros2_init)）
env -u PYTHONPATH .venv/bin/python -m policy_base.instance.entry \
    --config configs/pick_place/mock_ros2_env.yaml --dry-run

# 2) 只验证 mock 策略轮次与报告（不经过实例协议）
env -u PYTHONPATH .venv/bin/python -m policy_base.policy.mock \
    --report /tmp/policy_base_mock_report.json --steps 30 --chunk-size 5 --step-delay-s 0 --seed 0

# 3) 单测/冒烟（不连网、不连 ROS、不动真机）
env -u PYTHONPATH .venv/bin/python -m pytest tests/unit/test_instance_*.py tests/smoke -q
```

mock 后端只做确定性 chunk 推理与"模拟发布"，不 import rclpy/torch，不连串口/ROS/推理服务。

## 13 与 rule_base 实例契约一致性

| 契约点 | rule_base instance | policy_base instance |
|---|---|---|
| stopped 2 Hz latched（初始 True） | ✅ | ✅（频率来自 `instance.stopped_hz`，默认 2） |
| `env/reset_status` 严格 False→True 上升沿 | ✅ | ✅（同语义状态机） |
| 运行期 reset 丢弃、不排队 | ✅ | ✅ |
| 每轮 stopped=False→执行→True | ✅ | ✅ |
| 空闲/轮间 hold 2–5 Hz（实测关节） | ✅ | ✅（默认 4 Hz） |
| 单轮超时 + SIGTERM→宽限→SIGKILL | ✅ | ✅（`RoundCommand` 由 backend 构建） |
| SIGTERM/SIGINT → exit 0 清理 | ✅ | ✅ |
| 退出码 0/1/2 语义 | ✅ | ✅ |
| env 变量（NAMESPACE/DOMAIN_ID/STATION_TYPE/CLIENT_ID/LEASE_ID/INFERENCE_ID） | ✅ | ✅（namespace 变量名可配置） |
| 不 acquire/release 租约 | ✅ | ✅ |
| 每轮子进程 | `rule_base.interfaces.node --rounds 1` | `RoundCommand`（mock / finetune deploy） |

差异仅在"一轮做什么"：rule_base 跑 Pick 业务 CLI，policy_base 跑 ACT 策略子进程；
IM 可复用同一套 launch/stop/ready 编排。新增的实例 YAML schema 只影响本仓库入口。

## 14 未验证边界 / 已知限制

1. **真机未验证**：本仓库实例入口只做过 mock/离线证据（协议、报告、CLI、失败路径）。
   真机 Gate 需用户在场 + EP 一次性授权；未验证项包括 DDS 发现、arm_app 租约续期、
   finetune deploy 真实执行、相机/观测输入与 `published>0` 的实际运动效果。
2. **checkpoint 分发依赖 DVC**：`models/act-20261117-act-wrist-smoke` 由 IM `dvc pull`
   准备；未 pull 时 `preflight` 会报错（`build_round_command` 只校验安全门/配置，不读权重）。
3. **每轮一个子进程**：每轮有 Python/组件启动开销（mock 毫秒级；finetune 加载 checkpoint
   秒级），会话内复用属后续优化，不影响实例协议。
4. **warmup 只检查首个 joint_states 样本**：更完整的相机/服务就绪由平台/站端前置保证。
5. **失败即退出、不重试**：实例失败通过退出码 + 结构化事件归因，由 IM/FM 决定是否重拉；
   实例不做自动重试或环境 reset，也不判定任务结果。
6. **mock 通过 ≠ 真机通过**：mock 证据只证明协议/报告/清理路径，不证明真机安全或性能。

相关文档：`docs/policy-integration.md`（policy 后端/安全门/mock 细节）、
`docs/im-integration.md`（IM git 源 + DVC 分发）。
