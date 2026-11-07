"""
Shared LLM client for the MPLADS AI assistant
=============================================

The assistant's answer engines are deterministic; this module is the ONE
place that talks to a language model. The natural-language query engine uses
it for two narrow jobs only:

  1. propose a SQL statement for a question the deterministic planner does
     not recognise, and
  2. phrase an explanation of rows that were already retrieved.

Both are advisory. Nothing produced here is trusted: the caller validates SQL
against the strict validator + read-only executor, and the grounded
explanation template is used whenever a model response is missing, malformed
or contains numbers that were not in the result set.

Configuration (environment only — never in code or the response):
    MPLADS_LLM_API_KEY   or OPENAI_API_KEY   (absent => feature disabled)
    MPLADS_LLM_BASE_URL                     (default https://api.openai.com/v1)
    MPLADS_LLM_MODEL                        (default gpt-4o-mini)
    MPLADS_LLM_TIMEOUT                      (seconds, default 20)

No key value is ever logged, returned or included in an error message.
"""

import json
import os
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

_DEFAULT_BASE = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"

_state: Dict[str, Any] = {"client": None, "lock": threading.Lock()}


def api_key() -> Optional[str]:
    return os.environ.get("MPLADS_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or None


def base_url() -> str:
    return (os.environ.get("MPLADS_LLM_BASE_URL") or _DEFAULT_BASE).rstrip("/")


def model_name() -> str:
    return os.environ.get("MPLADS_LLM_MODEL") or _DEFAULT_MODEL


def timeout_seconds() -> float:
    try:
        return max(3.0, min(60.0, float(os.environ.get("MPLADS_LLM_TIMEOUT", "20"))))
    except (TypeError, ValueError):
        return 20.0


def available() -> bool:
    """True when a key is configured. Cheap; safe to call per request."""
    return bool(api_key())


def _post(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One bounded request. Returns parsed JSON or None; never raises."""
    key = api_key()
    if not key:
        return None
    req = urllib.request.Request(
        f"{base_url()}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds()) as resp:
            body = resp.read(400_000)
        return json.loads(body.decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        # Deliberately opaque: provider errors can echo request headers.
        return None


def complete_json(system: str, user: str, *, max_tokens: int = 700,
                  temperature: float = 0.0) -> Optional[Dict[str, Any]]:
    """Ask for a single JSON object. Returns {} on any failure."""
    if not available():
        return None
    payload = {
        "model": model_name(),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    data = _post(payload)
    if not data:
        return None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(content, dict):
        return content
    try:
        parsed = json.loads(str(content))
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None
