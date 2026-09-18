"""Computer control service — let psd.ai see and drive the local desktop.

This package is intentionally additive: importing it never touches input
devices, and every side-effecting entry point goes through
:func:`services.computer.policy.check_action`, which refuses anything that is
not on the allow-list.
"""

from .policy import (
    ActionRisk,
    READ_ONLY_ACTIONS,
    WRITE_ACTIONS,
    RISKY_ACTIONS,
    ALL_ACTIONS,
    check_action,
    action_risk,
    is_denied_target,
    describe_policy,
)
from .service import (
    ComputerService,
    ComputerActionError,
    get_computer_service,
)

__all__ = [
    "ActionRisk",
    "READ_ONLY_ACTIONS",
    "WRITE_ACTIONS",
    "RISKY_ACTIONS",
    "ALL_ACTIONS",
    "check_action",
    "action_risk",
    "is_denied_target",
    "describe_policy",
    "ComputerService",
    "ComputerActionError",
    "get_computer_service",
]
