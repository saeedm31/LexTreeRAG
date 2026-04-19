"""
Optional Gemini answer layer — uses Google Search grounding to provide
a web-sourced second opinion alongside the EUR-Lex CELLAR answer.

Only active when GEMINI_API_KEY is supplied.
Requires: pip install google-genai

Model selection: detected dynamically from the API at key-validation time
so we always use models actually available for the given key/tier.
"""

from __future__ import annotations
import re
from typing import Generator

try:
    from google import genai
    from google.genai import types as gtypes
    _GENAI_OK = True
except ImportError:
    _GENAI_OK = False


# Preference order for model selection — first substring match wins
_MODEL_PREFERENCE = [
    "gemini-2.0-flash-lite",   # free tier, newer
    "gemini-2.0-flash",        # paid / higher tier
    "gemini-1.5-flash-8b",     # free tier, lightweight
    "gemini-1.5-flash",        # free tier
    "gemini-1.5-pro",          # paid
    "gemini-pro",              # legacy fallback
]

_SYSTEM = """\
You are an EU legal research assistant.
Answer the user's question using Google Search to find relevant EUR-Lex documents,
official EU sources, and legal commentary.
Always cite your sources with URLs.
Be concise and structured.
"""


def _parse_retry_seconds(exc: Exception) -> int | None:
    msg = str(exc)
    for pattern in [r"retry[^\d]*(\d+)s", r"retryDelay.*?(\d+)s", r"in (\d+) second"]:
        m = re.search(pattern, msg, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def _is_rate_limit(exc: Exception) -> bool:
    return "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc)


def _detect_best_model(client) -> str | None:
    """
    List models available for this API key and return the best one
    for generateContent, based on _MODEL_PREFERENCE order.
    """
    try:
        available = [
            m.name.replace("models/", "")
            for m in client.models.list()
            if "generateContent" in (getattr(m, "supported_actions", None) or [])
            or hasattr(m, "supported_generation_methods") and
               "generateContent" in (m.supported_generation_methods or [])
        ]
        if not available:
            # Fallback: list all model names and filter by name pattern
            available = [
                m.name.replace("models/", "")
                for m in client.models.list()
            ]

        for pref in _MODEL_PREFERENCE:
            for name in available:
                if pref in name:
                    return name
        # Last resort: first model in list
        return available[0] if available else None
    except Exception:
        return None


def get_gemini_client(api_key: str):
    """Return a Gemini client or None if SDK not available / key invalid."""
    if not _GENAI_OK or not api_key:
        return None
    try:
        return genai.Client(api_key=api_key)
    except Exception:
        return None


def validate_gemini_key(api_key: str) -> tuple[bool, str]:
    """
    Validate the Gemini API key and detect the best available model.
    Returns (is_valid, model_name_or_error_message).
    """
    client = get_gemini_client(api_key)
    if not client:
        return False, "SDK not available"
    try:
        model = _detect_best_model(client)
        if model:
            return True, model
        return False, "No suitable model found"
    except Exception as exc:
        return False, str(exc)


def _make_config():
    return gtypes.GenerateContentConfig(
        system_instruction=_SYSTEM,
        tools=[gtypes.Tool(google_search=gtypes.GoogleSearch())],
    )


def gemini_answer_stream(
    question: str,
    client,
    model: str = "gemini-1.5-flash",
    auto_retry_limit: int = 90,   # auto-wait up to this many seconds
) -> Generator[str, None, None]:
    """
    Stream a Gemini answer with Google Search grounding.
    If rate-limited and the suggested wait is ≤ auto_retry_limit seconds,
    waits automatically and retries once — yielding a countdown message first.
    """
    import time

    if not _GENAI_OK or client is None:
        yield "_Gemini not available._"
        return

    def _try_stream():
        return client.models.generate_content_stream(
            model=model,
            contents=question,
            config=_make_config(),
        )

    for attempt in range(2):   # one auto-retry allowed
        try:
            for chunk in _try_stream():
                if chunk.text:
                    yield chunk.text
            return   # success

        except Exception as exc:
            if not _is_rate_limit(exc):
                yield f"\n\n_Gemini error: {exc}_"
                return

            wait = _parse_retry_seconds(exc) or 60

            if attempt == 0 and wait <= auto_retry_limit:
                # Auto-retry: yield a countdown message, then wait
                yield f"\n\n⏳ _Gemini rate limit — waiting **{wait}s** then retrying…_\n\n"
                time.sleep(wait)
                continue   # retry

            # Give up — show friendly message
            yield (
                f"\n\n> ⚠️ **Gemini rate limit reached** (`{model}`). "
                + (f"Please retry in **{wait} seconds**." if wait else "Please try again shortly.")
                + "\n> Free tier: ~10 req/min · 1,500 req/day."
            )
            return


def gemini_answer(
    question: str,
    client,
    model: str = "gemini-1.5-flash",
) -> dict:
    """
    Non-streaming Gemini answer with Google Search grounding.
    Returns {"text": str, "sources": [...], "model": str}
    """
    if not _GENAI_OK or client is None:
        return {"text": "_Gemini not available._", "sources": [], "model": ""}

    try:
        response = client.models.generate_content(
            model=model,
            contents=question,
            config=_make_config(),
        )
        text = response.text or ""

        sources = []
        try:
            meta = response.candidates[0].grounding_metadata
            if meta and meta.grounding_chunks:
                for chunk in meta.grounding_chunks:
                    if hasattr(chunk, "web") and chunk.web:
                        sources.append({
                            "title": chunk.web.title or chunk.web.uri,
                            "url":   chunk.web.uri,
                        })
        except Exception:
            pass

        return {"text": text, "sources": sources, "model": model}

    except Exception as exc:
        if _is_rate_limit(exc):
            wait = _parse_retry_seconds(exc)
            msg = (
                f"> ⚠️ **Gemini rate limit reached** (`{model}`). "
                + (f"Please retry in **{wait} seconds**." if wait else "Please try again shortly.")
                + "\n> Free tier: 1,500 requests/day · 15 req/min."
            )
            return {"text": msg, "sources": [], "model": model}
        return {"text": f"_Gemini error: {exc}_", "sources": [], "model": model}
