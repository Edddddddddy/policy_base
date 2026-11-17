"""SO-101 policy_base：inference_manager 可拉起的 ACT 策略实例仓库。

模块：

* :mod:`policy_base.instance` - FM/IM instance 协议（stopped/reset/hold/entry，对齐 rule_base 冻结契约）；
* :mod:`policy_base.policy` - ACT 策略集成（策略后端选择、mock/离线推理、checkpoint 预检、finetune 复用）。

本包顶层不 import ROS/torch/lerobot，离线单测无需 source ROS 2。
"""

from __future__ import annotations

__all__ = ["instance", "policy"]

__version__ = "0.1.0"
