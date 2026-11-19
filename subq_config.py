"""Strict, side-effect-free configuration parsing for the optional SubQ client.

This module does not read credentials, access MaiBot state, or perform network I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from urllib.parse import urlsplit


class SubQConfigError(ValueError):
    """Raised when SubQ settings are invalid; messages never include secret values."""


@dataclass(frozen=True)
class SubQConfig:
    enabled: bool = False
    protocol: str = "openai_chat_completions"
    provider: str = ""
    base_url: str = ""
    model: str = ""
    credential_ref: str = ""
    timeout_seconds: float = 20.0
    max_retries: int = 1
    daily_request_budget: int = 0
    daily_token_budget: int = 0
    daily_cost_budget: float = 0.0


def _bounded_number(data: dict, key: str, default, *, minimum: float, maximum: float,
                    integer: bool = False):
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SubQConfigError(f"{key} must be a number")
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise SubQConfigError(f"{key} must be between {minimum:g} and {maximum:g}")
    if integer and not isinstance(value, int):
        raise SubQConfigError(f"{key} must be an integer")
    return value


def _safe_base_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SubQConfigError("base_url is required when SubQ is enabled")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise SubQConfigError("base_url must not contain whitespace or control characters")
    value = value.strip().rstrip("/")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        _ = parts.port
    except ValueError:
        raise SubQConfigError("base_url is malformed") from None
    if parts.scheme not in {"https", "http"} or not host:
        raise SubQConfigError("base_url must be an absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        raise SubQConfigError("base_url must not contain user information")
    if parts.query or parts.fragment:
        raise SubQConfigError("base_url must not contain query or fragment")
    if parts.scheme == "http":
        local = host.lower() in {"localhost", "127.0.0.1", "::1"}
        if not local:
            raise SubQConfigError("base_url must use HTTPS except for loopback development")
    return value


def parse_subq_config(raw: dict | None) -> SubQConfig:
    """Parse a [subq] mapping; defaults to disabled and never resolves secrets."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SubQConfigError("subq settings must be a table")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise SubQConfigError("enabled must be a boolean")
    protocol = raw.get("protocol", "openai_chat_completions")
    if protocol != "openai_chat_completions":
        raise SubQConfigError("protocol must be openai_chat_completions")

    provider = raw.get("provider", "")
    model = raw.get("model", "")
    credential_ref = raw.get("credential_ref", "")
    for key, value in (("provider", provider), ("model", model), ("credential_ref", credential_ref)):
        if not isinstance(value, str):
            raise SubQConfigError(f"{key} must be a string")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise SubQConfigError(f"{key} must not contain control characters")
        if key == "model" and (len(value) > 256 or any(ch.isspace() for ch in value)):
            raise SubQConfigError("model must be a non-empty identifier without whitespace (max 256 chars)")
    if credential_ref and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", credential_ref):
        raise SubQConfigError("credential_ref must be an environment variable name")

    timeout = _bounded_number(raw, "timeout_seconds", 20.0, minimum=1, maximum=120)
    retries = _bounded_number(raw, "max_retries", 1, minimum=0, maximum=1, integer=True)
    requests = _bounded_number(raw, "daily_request_budget", 0, minimum=0, maximum=100000, integer=True)
    tokens = _bounded_number(raw, "daily_token_budget", 0, minimum=0, maximum=100000000, integer=True)
    cost = _bounded_number(raw, "daily_cost_budget", 0.0, minimum=0, maximum=1000000)

    base_url = raw.get("base_url", "")
    if not isinstance(base_url, str):
        raise SubQConfigError("base_url must be a string")
    if enabled:
        if not provider.strip():
            raise SubQConfigError("provider is required when SubQ is enabled")
        if not model.strip():
            raise SubQConfigError("model is required when SubQ is enabled")
        if not credential_ref.strip():
            raise SubQConfigError("credential_ref is required when SubQ is enabled")
        base_url = _safe_base_url(base_url)
    elif base_url.strip():
        # Validate any configured endpoint even while disabled, so enabling later is predictable.
        base_url = _safe_base_url(base_url)

    return SubQConfig(
        enabled=enabled, protocol=protocol, provider=provider.strip(), base_url=base_url,
        model=model.strip(), credential_ref=credential_ref.strip(),
        timeout_seconds=float(timeout), max_retries=retries,
        daily_request_budget=requests, daily_token_budget=tokens, daily_cost_budget=float(cost),
    )
