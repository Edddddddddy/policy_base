"""policy_base instance mode: protocol skeleton reused from the frozen rule_base contract.

Modules:

* :mod:`policy_base.instance.protocol` - pure env parsing, the False->True reset edge
  gate and exit-code classification (no ROS imports);
* :mod:`policy_base.instance.keepalive` - pure hold-command builder and the 2-5 Hz
  heartbeat state used to keep the arm lease alive while idle;
* :mod:`policy_base.instance.runner` - one-round policy child process: frozen
  ``RoundCommand`` startup, single-round timeout, SIGTERM forwarding, report reading;
* :mod:`policy_base.instance.config` - instance YAML loader (timings/topics), with the
  ``policy:`` section delegated to :mod:`policy_base.policy.config`;
* :mod:`policy_base.instance.entry` - the rclpy entrypoint launched by the inference
  manager (``python3 -m policy_base.instance.entry --config <yaml> [--dry-run]``).

Docs: ``policy_base/docs/instance-mode.md``.
"""

from __future__ import annotations

__all__ = ["config", "entry", "keepalive", "protocol", "runner"]
