"""ACT 策略集成层：配置、轮次命令构建、mock/离线推理、checkpoint 预检、finetune 复用。

本包不 import torch/lerobot/rclpy：真实推理由 finetune 仓的稳定命令面
（``deploy/run.sh execute``）在子进程中执行；本包只负责选择后端、校验安全门、
构建 argv/env、解析轮次报告。

模块：

* :mod:`policy_base.policy.config` - ``policy:`` 配置段解析（严格、纯逻辑）；
* :mod:`policy_base.policy.backend` - 轮次命令构建/报告解析（instance 入口的唯一调用面）；
* :mod:`policy_base.policy.mock` - 确定性 mock 策略（离线 chunk 推理 + 子进程入口）；
* :mod:`policy_base.policy.finetune` - finetune 复用（版本 pin、checkpoint 目录解析、deploy 命令）；
* :mod:`policy_base.policy.preflight` - 离线 checkpoint/环境预检 CLI。
"""

from __future__ import annotations

__all__ = ["backend", "config", "finetune", "mock", "preflight"]
