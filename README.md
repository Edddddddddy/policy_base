# policy_base

SO-101 的 **IM（inference_manager）实例化 ACT 策略仓库**，是 `rule_base` 之外的第二种推理执行交付物：
EP 下发任务 → FM → IM 拉起本仓库实例 → 站端 runner 驱动 `/arm/joint_command`（真机执行）。

> 规划与验收：`../.scratch/session-15-platform-diag/PLAN-e2e-closed-loop.md`（P3 部分）
> 编码约定：本仓库 `AGENTS.md`（参考 rule_base 的 AGENTS.md，按本仓库边界调整）
> 状态：**骨架待创建**（P3 开发中，2026-11-17 初始化）

## 边界
- 只做：instance 协议实现 + ACT 策略加载/推理/站端执行集成 + DVC 权重分发。
- 不做：训练（finetune）、数据采集/质检（so101-station/EP）、录制/评测（recorder/env/benchmark）、平台业务。
- 安全：真机执行必须用户在场 + EP 一次性授权；`--enable-motion`/`--confirm-real` 不可绕过；不放松安全阈值。

## 待办（P3，详见计划 §3）
1. 复用 rule_base instance skeleton，落地 `src/policy_base/instance/`；
2. ACT 策略集成（policy server + runner，复用 finetune 能力）；
3. `submission.yaml`（policy-based + GPU resources + uv argv）、`uv.lock`；
4. DVC：`dvc init`，MinIO remote，checkpoint 入 DVC，`dvc push`；
5. IM 支持（P3-4：git 源 + DVC，或本地模式 DVC 开关，先 spike）；
6. EP 模板/A1 alias/FM pin 接线 → mock→shadow→真机单轮；
7. 迁移完成后废弃临时 `act_instance_repo`。
