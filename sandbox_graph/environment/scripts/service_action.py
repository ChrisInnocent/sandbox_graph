"""Allow-listed systemd actions with independent active-state verification."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ServiceAction:
    action_ref: str
    unit: str
    verb: str
    timeout_seconds: int = 30

    def __post_init__(self):
        if not self.action_ref.endswith(".v1") or not self.unit.endswith(".service"):
            raise ValueError("service action identity is invalid")
        if self.verb not in {"start", "restart", "reload"} or not 1 <= self.timeout_seconds <= 300:
            raise ValueError("service action is outside supported bounds")


class ServiceActionRegistry:
    def __init__(self, actions: tuple[ServiceAction, ...], *, runner: Callable = subprocess.run):
        self.actions = {action.action_ref: action for action in actions}
        if len(self.actions) != len(actions):
            raise ValueError("duplicate service action registration")
        self.runner = runner

    def execute(self, action_ref: str) -> dict:
        action = self.actions.get(action_ref)
        if action is None:
            return {"status": "blocked", "reason": "action_not_registered"}
        try:
            result = self.runner(["systemctl", action.verb, action.unit], check=False,
                capture_output=True, text=True, timeout=action.timeout_seconds, shell=False)
        except subprocess.TimeoutExpired:
            return {"status": "unknown", "reason": "service_action_timeout"}
        if result.returncode != 0:
            return {"status": "failed", "reason": "service_action_nonzero", "exit_code": result.returncode}
        try:
            verification = self.runner(["systemctl", "is-active", "--quiet", action.unit], check=False,
                capture_output=True, text=True, timeout=min(action.timeout_seconds, 30), shell=False)
        except subprocess.TimeoutExpired:
            return {"status": "unknown", "reason": "service_verification_timeout"}
        if verification.returncode != 0:
            return {"status": "postcondition_failed", "reason": "service_not_active"}
        return {"status": "verified", "unit": action.unit, "verb": action.verb}
