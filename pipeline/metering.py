"""
Count the tokens of every model call without touching the pipeline code.

`MeteredClient` wraps an `anthropic.Anthropic` client and `MeteredGemini` wraps a
`google.genai` client. Each call's token counts go into a shared `Meter`, labelled
with the pipeline step that made the call (found from the calling function's name).
The pipeline keeps calling `client.messages.create(...)` / `.stream(...)` as before.
"""

from __future__ import annotations

import sys
import threading

from .costs import Usage, estimate_tokens, from_anthropic, from_gemini

# Calling function -> step label. The innermost match wins, except uploads (checked first).
STEP_OF = {
    "classify_question": "Sort the question",
    "extract_keywords_and_year": "Pick search words",
    "_summarise_batch": "Index new laws",
    "summarise_articles": "Index new laws",
    "navigate_tree": "Choose articles",
    "answer_stream": "Write the answer",
    "answer_structured": "Write the answer",
    "answer_prose": "Write the answer",
    "run_pipeline": "Write the answer",
    "compute_confidence": "Check the answer",
}
UPLOAD_FUNCS = {"ingest_uploaded_file", "build_upload_tree"}

# Steps whose cost is paid once per law, not per question.
ONE_TIME_STEPS = {"Index new laws", "Index uploads"}


def current_step(max_depth: int = 25) -> str:
    """Label of the pipeline step that is calling the model."""
    names = []
    frame = sys._getframe(1)
    while frame is not None and len(names) < max_depth:
        names.append(frame.f_code.co_name)
        frame = frame.f_back
    if UPLOAD_FUNCS.intersection(names):
        return "Index uploads"
    for name in names:
        if name in STEP_OF:
            return STEP_OF[name]
    return "Other"


class Meter:
    """All model calls made in one browser session (thread-safe)."""

    def __init__(self):
        self.calls: list[Usage] = []
        self._lock = threading.Lock()

    def add(self, usage: Usage) -> Usage:
        with self._lock:
            self.calls.append(usage)
        return usage

    def mark(self) -> int:
        return len(self.calls)

    def since(self, mark: int) -> list[Usage]:
        return list(self.calls[mark:])


def _text_of(messages, system=None) -> str:
    parts = [system if isinstance(system, str) else ""]
    for m in messages or []:
        c = m.get("content", "")
        parts.append(c if isinstance(c, str) else " ".join(b.get("text", "") for b in c if isinstance(b, dict)))
    return "".join(parts)


class _Messages:
    def __init__(self, inner, meter: Meter, model_map: dict | None = None):
        self._inner, self._meter = inner, meter
        self._map = model_map or {}

    def _api(self, kwargs: dict) -> dict:
        """The same call with the provider's model ID (e.g. Bedrock); usage keeps the plain model name."""
        model = kwargs.get("model")
        return dict(kwargs, model=self._map[model]) if model in self._map else kwargs

    def create(self, **kwargs):
        step = current_step()
        resp = self._inner.create(**self._api(kwargs))
        u = from_anthropic(resp, kwargs.get("model", "?"), step)
        if u.estimated:
            u.input = estimate_tokens(_text_of(kwargs.get("messages"), kwargs.get("system")))
        self._meter.add(u)
        return resp

    def stream(self, **kwargs):
        return _MeteredStream(self._inner.stream(**self._api(kwargs)), kwargs, current_step(), self._meter)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _MeteredStream:
    """Context manager around `messages.stream(...)`; records usage from the final message."""

    def __init__(self, manager, kwargs, step, meter):
        self._manager, self._kwargs, self._step, self._meter = manager, kwargs, step, meter
        self._stream = None

    def __enter__(self):
        self._stream = self._manager.__enter__()
        return self._stream

    def __exit__(self, *exc):
        model = self._kwargs.get("model", "?")
        try:
            final = self._stream.get_final_message() if exc[0] is None else \
                getattr(self._stream, "current_message_snapshot", None)
            u = from_anthropic(final, model, self._step)
        except Exception:
            u = Usage(model, self._step, estimated=True)
        if u.estimated:
            u.input = estimate_tokens(_text_of(self._kwargs.get("messages"), self._kwargs.get("system")))
        self._meter.add(u)
        return self._manager.__exit__(*exc)


class MeteredClient:
    """Drop-in for `anthropic.Anthropic`: same calls, every one counted in `meter`.
    `model_map` translates model names for another endpoint (e.g. Amazon Bedrock IDs)."""

    def __init__(self, client, meter: Meter | None = None, model_map: dict | None = None):
        self._client = client
        self.meter = meter or Meter()
        self.messages = _Messages(client.messages, self.meter, model_map)

    def __getattr__(self, name):
        return getattr(self._client, name)


class _GeminiModels:
    def __init__(self, inner, meter: Meter):
        self._inner, self._meter = inner, meter

    def generate_content(self, **kwargs):
        resp = self._inner.generate_content(**kwargs)
        self._meter.add(from_gemini(getattr(resp, "usage_metadata", None), kwargs.get("model", "?")))
        return resp

    def generate_content_stream(self, **kwargs):
        model = kwargs.get("model", "?")
        last = None
        text = []
        try:
            for chunk in self._inner.generate_content_stream(**kwargs):
                if getattr(chunk, "usage_metadata", None) is not None:
                    last = chunk.usage_metadata
                text.append(getattr(chunk, "text", None) or "")
                yield chunk
        finally:
            u = from_gemini(last, model)
            if u.estimated:
                u.input = estimate_tokens(str(kwargs.get("contents", "")))
                u.output = estimate_tokens("".join(text))
            if last is not None or text:
                self._meter.add(u)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class MeteredGemini:
    """Drop-in for a `google.genai` client: generate_content calls are counted in `meter`."""

    def __init__(self, client, meter: Meter):
        self._client = client
        self.meter = meter
        self.models = _GeminiModels(client.models, meter)

    def __getattr__(self, name):
        return getattr(self._client, name)
