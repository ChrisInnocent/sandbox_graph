"""Explicit service-side credential lifecycle and scoped client bindings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .contracts import AuthBinding
from .secrets import SecretStore


@dataclass(frozen=True)
class AuthProvisionResult:
    plane: str
    mode: str
    credential_ref: str
    credential_version: str
    created: bool
    previous_ref: str | None = None


def _parts(ref: str) -> tuple[str, str, str]:
    match = ref.split("/")
    if len(match) != 5 or match[0] != "secret:" or match[1] != "":
        raise ValueError("invalid credential reference")
    return match[2], match[3], match[4]


def provision_binding(
    binding: AuthBinding,
    store: SecretStore,
    *,
    scope: str,
    name: str,
    factory: Callable[[], str],
    previous_ref: str | None = None,
) -> AuthProvisionResult:
    """Resolve, create once, or create a separately versioned rotated secret."""
    if binding.provision == "reuse":
        bound_scope, bound_name, version = _parts(binding.credential_ref)
        if (bound_scope, bound_name) != (scope, name):
            raise PermissionError("credential ref does not match environment scope")
        store.resolve(binding.credential_ref, scope=scope)
        return AuthProvisionResult(binding.plane, "reuse", binding.credential_ref,
                                   binding.credential_version or version, False)
    version = binding.credential_version
    if not version:
        raise ValueError("explicit new credential_version is required")
    expected_ref = f"secret://{scope}/{name}/{version}"
    if binding.credential_ref != expected_ref:
        raise ValueError("credential_ref does not match requested scope and version")
    if binding.provision == "rotate":
        if not previous_ref:
            raise ValueError("rotation requires the previous credential ref")
        previous_scope, previous_name, _ = _parts(previous_ref)
        if (previous_scope, previous_name) != (scope, name) or previous_ref == expected_ref:
            raise ValueError("rotation previous credential must be a distinct version in the same scope")
        store.resolve(previous_ref, scope=scope)
    item = store.create_if_absent(scope, name, factory, version=version)
    store.resolve(item.ref, scope=scope)
    return AuthProvisionResult(binding.plane, binding.provision, item.ref,
                               version, item.created, previous_ref)


def resolve_credential(binding: AuthProvisionResult, store: SecretStore, *, scope: str) -> str:
    """Read a secret only at the executor boundary; callers must not persist it."""
    if binding.credential_ref.split("/")[2] != scope:
        raise PermissionError("credential scope mismatch")
    return store.resolve(binding.credential_ref, scope=scope)
