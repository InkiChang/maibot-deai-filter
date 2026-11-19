"""Fail-closed extraction of a text-only personal message envelope."""
from __future__ import annotations

from typing import Any


def private_text_envelope(message: Any) -> tuple[str, str, str, str, str] | None:
    """Return platform, user, stream, message id, text; reject ambiguous input."""
    if not isinstance(message, dict):
        return None
    if message.get("group_info") is not None or message.get("group_id"):
        return None
    info = message.get("message_info")
    if not isinstance(info, dict) or info.get("group_info") is not None:
        return None
    user = info.get("user_info")
    if not isinstance(user, dict):
        return None
    platform = message.get("platform")
    user_id = user.get("user_id")
    stream_id = message.get("session_id")
    message_id = message.get("message_id")
    parts = message.get("raw_message")
    if not all(isinstance(v, str) and v.strip() for v in (platform, user_id, stream_id, message_id)):
        return None
    if not isinstance(parts, list) or not parts:
        return None
    text_parts: list[str] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "text":
            return None
        data = part.get("data")
        if isinstance(data, str):
            text_parts.append(data)
        elif isinstance(data, dict) and isinstance(data.get("text"), str):
            text_parts.append(data["text"])
        else:
            return None
    text = "".join(text_parts).strip()
    if not text or len(text) > 4000:
        return None
    return platform, user_id, stream_id, message_id, text
