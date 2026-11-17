"""测试共享夹具（policy_base）。

默认全部离线：不 import ROS、不连 MinIO、不启动真机。需要 ROS/网络/权重的用例必须显式
mark（如 ``integration``）或在缺少环境时 skip，保证 CI 与本地默认路径可重复。
"""

from __future__ import annotations
