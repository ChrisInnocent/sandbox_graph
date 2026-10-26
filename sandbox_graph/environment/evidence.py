"""Redact secret-bearing evidence before it enters artifacts or agent context."""
from __future__ import annotations

import re
import json
from typing import TYPE_CHECKING
from typing import Any, Iterable, Mapping

if TYPE_CHECKING:
    from ..artifacts.store import FileArtifactStore
    from ..domain.models import EvidenceRef

_SENSITIVE_KEY = re.compile(r"(?:authorization|x-api-key|api[_-]?key(?!_env)|api[_-]?secret|manifest[_-]?key|route[_-]?key|access[_-]?token|traffic[_-]?token|password|private[_-]?key|secret[_-]?value)", re.IGNORECASE)
_PRIVATE_MATERIAL = re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----|^\s*(?:authorization|x-api-key|e2b-traffic-access-token)\s*:", re.IGNORECASE | re.MULTILINE)


class SecretRedactor:
    def __init__(self, secret_values: Iterable[str] = ()):
        self._values = tuple(sorted({v for v in secret_values if isinstance(v, str) and len(v) >= 4}, key=len, reverse=True))

    def redact_text(self, value: str) -> str:
        for secret in self._values:
            value = value.replace(secret, "[REDACTED]")
        return value

    def redact(self, value: Any, *, key: str | None = None) -> Any:
        if key and _SENSITIVE_KEY.search(key):
            return "[REDACTED]"
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            return {str(item_key): self.redact(item, key=str(item_key)) for item_key, item in value.items()}
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value

    def sanitize_exception(self, error: BaseException) -> str:
        return self.redact_text(f"{type(error).__name__}: {error}")


def assert_public_payload_safe(value: Any, *, secret_values: Iterable[str] = ()) -> None:
    values = tuple(secret_values)

    def visit(item: Any, key: str | None = None):
        if key and _SENSITIVE_KEY.search(key) and item != "[REDACTED]":
            raise ValueError(f"sensitive field in public payload: {key}")
        if isinstance(item, str):
            if any(secret and secret in item for secret in values):
                raise ValueError("secret material found in public payload")
            if _PRIVATE_MATERIAL.search(item):
                raise ValueError("secret material found in public payload")
            return
        if isinstance(item, Mapping):
            for child_key, child in item.items():
                visit(child, str(child_key))
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)


def commit_sanitized_json(store: "FileArtifactStore", value: Any, *, scope: str,
                          redactor: SecretRedactor, producer: str) -> "EvidenceRef":
    sanitized = redactor.redact(value)
    assert_public_payload_safe(sanitized)
    payload = json.dumps(sanitized, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return store.commit([payload], scope=scope, media_type="application/json", producer=producer)


__all__ = ["SecretRedactor", "assert_public_payload_safe", "commit_sanitized_json"]
