"""Digest-locked install actions; commands and verification rules are registered code."""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class InstallAction:
    action_ref: str
    artifact_path: str
    artifact_sha256: str
    argv: tuple[str, ...]
    timeout_seconds: int
    verification_rule: str

    def __post_init__(self):
        if not self.action_ref.endswith(".v1") or not self.argv or not self.verification_rule:
            raise ValueError("install action identity is incomplete")
        if not 1 <= self.timeout_seconds <= 3600 or len(self.artifact_sha256) != 64:
            raise ValueError("install bounds or artifact digest are invalid")
        if any("\x00" in arg for arg in self.argv):
            raise ValueError("install argv contains invalid bytes")


class ProductInstallRegistry:
    def __init__(self, actions: tuple[InstallAction, ...], *, runner: Callable = subprocess.run,
                 verify: Callable[[str], bool]):
        self.actions = {action.action_ref: action for action in actions}
        if len(self.actions) != len(actions):
            raise ValueError("duplicate product install action")
        self.runner, self.verify = runner, verify

    def execute(self, action_ref: str) -> dict:
        action = self.actions.get(action_ref)
        if action is None:
            return {"status": "blocked", "reason": "action_not_registered"}
        artifact = Path(action.artifact_path)
        if artifact.is_symlink() or not artifact.is_file():
            return {"status": "blocked", "reason": "locked_artifact_missing_or_unsafe"}
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if digest != action.artifact_sha256:
            return {"status": "artifact_digest_mismatch"}
        try:
            result = self.runner(list(action.argv), check=False, capture_output=True,
                text=True, timeout=action.timeout_seconds, shell=False)
        except subprocess.TimeoutExpired:
            return {"status": "unknown", "reason": "install_timeout"}
        if result.returncode != 0:
            return {"status": "failed", "reason": "install_nonzero", "exit_code": result.returncode}
        try:
            verified = self.verify(action.verification_rule)
        except Exception:
            verified = False
        if not verified:
            return {"status": "postcondition_failed", "reason": "independent_verification_failed"}
        return {"status": "verified", "artifact_sha256": digest, "verification_rule": action.verification_rule}
