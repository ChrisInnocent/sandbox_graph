"""Bounded positive/negative authentication proof for protected endpoints."""
from __future__ import annotations

import inspect
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable


@dataclass(frozen=True)
class AuthResponse:
    status_code: int
    body: Any = None


@dataclass(frozen=True)
class AuthProbeResult:
    status: str
    endpoint: str | None
    missing_status: int | None
    wrong_status: int | None
    correct_status: int | None
    ownership_verified: bool | None
    evidence: tuple[str, ...] = ()


class AuthProbe:
    def __init__(self, call: Callable[[str | None], Awaitable[AuthResponse] | AuthResponse]):
        self.call = call

    async def _call(self, credential: str | None) -> AuthResponse:
        response = self.call(credential)
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, AuthResponse):
            response = AuthResponse(int(response.status_code), getattr(response, "body", None))
        return response

    async def verify(
        self, *, correct_credential: str, rejected_statuses: Iterable[int],
        accepted_statuses: Iterable[int], wrong_credential: str | None = None,
        ownership_check: Callable[[AuthResponse], bool] | None = None,
        health_endpoint: bool = False, endpoint: str | None = None,
    ) -> AuthProbeResult:
        if health_endpoint:
            return AuthProbeResult("health_only", endpoint, None, None, None, None)
        if not correct_credential:
            return AuthProbeResult("credential_unavailable", endpoint, None, None, None, None)
        wrong = wrong_credential or secrets.token_urlsafe(32)
        if not wrong or secrets.compare_digest(wrong, correct_credential):
            return AuthProbeResult("invalid_probe_configuration", endpoint, None, None, None, None)
        rejected, accepted = set(rejected_statuses), set(accepted_statuses)
        if not rejected or not accepted or rejected & accepted:
            return AuthProbeResult("invalid_probe_configuration", endpoint, None, None, None, None)
        try:
            missing = await self._call(None)
            invalid = await self._call(wrong)
            valid = await self._call(correct_credential)
        except Exception:
            return AuthProbeResult("endpoint_unavailable", endpoint, None, None, None, None)
        if missing.status_code in accepted:
            return AuthProbeResult("unprotected", endpoint, missing.status_code,
                                   invalid.status_code, valid.status_code, None)
        if invalid.status_code in accepted:
            return AuthProbeResult("wrong_credential_accepted", endpoint, missing.status_code,
                                   invalid.status_code, valid.status_code, None)
        if missing.status_code not in rejected or invalid.status_code not in rejected:
            return AuthProbeResult("rejection_semantics_unknown", endpoint, missing.status_code,
                                   invalid.status_code, valid.status_code, None)
        if valid.status_code not in accepted:
            return AuthProbeResult("correct_credential_rejected", endpoint, missing.status_code,
                                   invalid.status_code, valid.status_code, None)
        ownership = ownership_check(valid) if ownership_check else None
        if ownership_check and not ownership:
            return AuthProbeResult("ownership_unverified", endpoint, missing.status_code,
                                   invalid.status_code, valid.status_code, False)
        return AuthProbeResult("verified", endpoint, missing.status_code,
                               invalid.status_code, valid.status_code, ownership)


__all__ = ["AuthProbe", "AuthProbeResult", "AuthResponse"]
