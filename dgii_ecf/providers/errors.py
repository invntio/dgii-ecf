"""Framework-free error and redaction contract for every provider adapter."""

from __future__ import annotations

import re
from typing import Any

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "credential",
    "cookie",
)
_SENSITIVE_LABEL_PATTERN = (
    r"(?:api[ _-]?key|token|password|passwd|secret|authorization|credential|cookie)"
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[a-z0-9._~+\-/]+=*")
_QUOTED_CREDENTIAL_RE = re.compile(
    rf"(?i)([\"'][^\"']*{_SENSITIVE_LABEL_PATTERN}[^\"']*[\"']\s*[:=]\s*)"
    r"([\"'][^\"']*[\"']|[^\s,;}]+)"
)
_BARE_CREDENTIAL_RE = re.compile(
    rf"(?i)({_SENSITIVE_LABEL_PATTERN}\s*[:=]\s*)"
    r"([\"'][^\"']*[\"']|[^\s,;}]+)"
)


def _is_sensitive_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def sanitize_provider_text(value: Any) -> str:
    """Redact common credential forms from untrusted provider text."""
    text = str(value or "")
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _QUOTED_CREDENTIAL_RE.sub(r"\1[REDACTED]", text)
    return _BARE_CREDENTIAL_RE.sub(r"\1[REDACTED]", text)


def sanitize_provider_data(value: Any):
    """Recursively redact sensitive keys and strings without framework imports."""
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if _is_sensitive_key(key)
                else sanitize_provider_data(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_provider_data(item) for item in value]
    if isinstance(value, str):
        return sanitize_provider_text(value)
    return value


def safe_provider_payload_text(value: Any, max_chars: int = 2000) -> str:
    """Return a bounded representation suitable for exceptions and logs."""
    return str(sanitize_provider_data(value))[:max_chars]


class ProviderError(Exception):
    """Base error raised by a provider transport or adapter."""


class ProviderConnectionError(ProviderError):
    """The remote request outcome is unknown because transport failed."""


class ProviderAuthError(ProviderError):
    """Provider credentials or permissions were rejected."""

    def __init__(self, message: str, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


class ProviderHTTPError(ProviderError):
    """Non-authentication HTTP failure returned by a provider."""

    def __init__(self, status_code: int, payload, provider: str = "Provider"):
        self.status_code = status_code
        self.payload = sanitize_provider_data(payload)
        super().__init__(
            f"{provider} HTTP {status_code}: "
            f"{safe_provider_payload_text(self.payload)}"
        )
