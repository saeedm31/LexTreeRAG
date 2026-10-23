"""
What each model call cost: token counts from the provider's reply, priced in euros.

Token counts are exact when the provider returns them (Anthropic and Gemini both do).
The euro amount is an estimate: list price per million tokens x tokens, converted
from US dollars. The provider's bill is the real number.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import os

PRICES_CHECKED = "22 October 2026"
USD_TO_EUR = 0.89          # 1 USD in EUR on 21 Oct 2026; editable in the app

# Multiplies every list price. Amazon Bedrock's in-region endpoints cost 10% more than global ones,
# so the AWS deployment sets PRICE_FACTOR=1.10.
PRICE_FACTOR = float(os.getenv("PRICE_FACTOR", "1"))

# USD per 1 million tokens: input, cache write (5 min), cache read, output.
# Anthropic: platform.claude.com/docs/en/about-claude/pricing
# Gemini: ai.google.dev/gemini-api/docs/pricing (no cache-write surcharge, so write = input)
PRICES = {
    "claude-opus-4-6":   (5.00, 6.25, 0.50, 25.00),
    "claude-sonnet-4-6": (3.00, 3.75, 0.30, 15.00),
    "claude-haiku-4-5":  (1.00, 1.25, 0.10, 5.00),
    "claude-opus-5-5":   (4.00, 5.00, 0.20, 20.00),
    "claude-sonnet-5-5": (2.00, 2.50, 0.10, 10.00),
    "claude-haiku-5-5":  (0.10, 0.125, 0.01, 0.50),
    "gemini-3.8-flash":      (0.75, 0.75, 0.075, 3.75),   # price until 31 Dec 2026
    "gemini-3.6-flash":      (0.75, 0.75, 0.075, 3.75),
    "gemini-3.5-flash-lite": (0.30, 0.30, 0.03, 2.50),
    "gemini-3.1-flash-lite": (0.25, 0.25, 0.025, 1.50),
}

# Prompts above this many tokens pay a higher rate for these models.
LONG_PROMPT = {"claude-haiku-5-5": (100_000, (0.50, 0.625, 0.05, 2.50))}

# Models the answer can be written with; thinking setting each one accepts.
ANSWER_MODELS = {
    "claude-opus-4-6":   "Claude Opus 4.6 (default)",
    "claude-opus-5-5":   "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-5-5":  "Claude Haiku 5.5",
    "claude-haiku-4-5":  "Claude Haiku 4.5",
}


def thinking_for(model: str):
    """Thinking setting for the answer model: adaptive where supported, none on Haiku 4.5."""
    return None if model.startswith("claude-haiku-4-5") else {"type": "adaptive"}


@dataclass
class Usage:
    """Tokens used by one model call."""
    model: str
    step: str = "Other"
    input: int = 0           # input tokens billed at the normal rate
    output: int = 0          # includes thinking tokens
    cache_read: int = 0      # input tokens read from the prompt cache (cheap)
    cache_write: int = 0     # input tokens written to the prompt cache
    estimated: bool = False  # no counts from the provider: guessed from text length

    @property
    def tokens(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Usage":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def estimate_tokens(text: str) -> int:
    """Rough count for English text: about 4 characters per token."""
    return max(1, round(len(text or "") / 4))


def _get(obj, name, default=0):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def from_anthropic(response, model: str, step: str = "Other") -> Usage:
    """Usage from an Anthropic Messages reply (object or dict)."""
    u = _get(response, "usage", None)
    if u is None:
        return Usage(model, step, estimated=True)
    return Usage(model, step,
                 input=int(_get(u, "input_tokens") or 0),
                 output=int(_get(u, "output_tokens") or 0),
                 cache_read=int(_get(u, "cache_read_input_tokens") or 0),
                 cache_write=int(_get(u, "cache_creation_input_tokens") or 0))


def from_gemini(usage_metadata, model: str, step: str = "Gemini (web search)") -> Usage:
    """Usage from a Gemini reply's usage_metadata. Thinking tokens are billed as output."""
    if usage_metadata is None:
        return Usage(model, step, estimated=True)
    prompt = int(_get(usage_metadata, "prompt_token_count") or 0)
    cached = int(_get(usage_metadata, "cached_content_token_count") or 0)
    out = int(_get(usage_metadata, "candidates_token_count") or 0) \
        + int(_get(usage_metadata, "thoughts_token_count") or 0)
    return Usage(model, step, input=max(prompt - cached, 0), output=out, cache_read=cached)


def price_for(model: str, prices: dict = PRICES):
    """Exact match, else the longest known name the model starts with (dated versions)."""
    if model in prices:
        return prices[model]
    hits = [k for k in prices if model.startswith(k)]
    return prices[max(hits, key=len)] if hits else None


def cost_eur(usage: Usage, prices: dict = PRICES, usd_to_eur: float = USD_TO_EUR):
    """Euros for one call, or None when the model's price is unknown."""
    p = price_for(usage.model, prices)
    if p is None:
        return None
    for name, (limit, long_price) in LONG_PROMPT.items():
        if usage.model.startswith(name) and usage.input + usage.cache_read + usage.cache_write > limit:
            p = long_price
    inp, write, read, out = p
    usd = (usage.input * inp + usage.cache_write * write + usage.cache_read * read + usage.output * out) / 1e6
    return usd * usd_to_eur * PRICE_FACTOR


def total(usages, prices: dict = PRICES, usd_to_eur: float = USD_TO_EUR) -> dict:
    """Sum of tokens and euros for a list of calls; euros are None if any price is unknown."""
    usages = list(usages)
    eur = 0.0
    for u in usages:
        c = cost_eur(u, prices, usd_to_eur)
        if c is None:
            eur = None
            break
        eur += c
    return {"calls": len(usages), "input": sum(u.input for u in usages), "output": sum(u.output for u in usages),
            "cache_read": sum(u.cache_read for u in usages), "cache_write": sum(u.cache_write for u in usages),
            "eur": eur, "estimated": any(u.estimated for u in usages)}


def by_step(usages, prices: dict = PRICES, usd_to_eur: float = USD_TO_EUR) -> list[dict]:
    """Totals per pipeline step, most expensive first."""
    groups: dict[str, list] = {}
    for u in usages:
        groups.setdefault(u.step, []).append(u)
    rows = [{"step": s, **total(us, prices, usd_to_eur),
             "models": sorted({u.model for u in us})} for s, us in groups.items()]
    return sorted(rows, key=lambda r: -(r["eur"] or 0))


def eur(v) -> str:
    """Small amounts need more decimals: €0.0004, €0.012, €1.23."""
    if v is None:
        return "price unknown"
    if v == 0:
        return "€0"
    if abs(v) < 0.0001:
        return "< €0.0001"
    if abs(v) < 0.01:
        return f"€{v:.4f}"
    if abs(v) < 1:
        return f"€{v:.3f}"
    return f"€{v:,.2f}"


# ── Index cost of a law that came from the cache ─────────────────────────────

_SUMMARY_BATCH = 12        # same batch size as tree_builder


def index_usage_estimate(tree: dict) -> list[Usage]:
    """What indexing this law cost (or would cost): its recorded usage, else an estimate from its articles."""
    recorded = tree.get("index_usage")
    if recorded:
        return [Usage.from_dict(d) for d in recorded]
    nodes = tree.get("nodes", [])
    calls = []
    for i in range(0, len(nodes), _SUMMARY_BATCH):
        batch = nodes[i:i + _SUMMARY_BATCH]
        chars = 600 + len(tree.get("title", "")) + sum(min(len(n.get("text", "")), 800) + len(n.get("title", "")) + 20
                                                       for n in batch)
        calls.append(Usage("claude-haiku-4-5", "Index new laws", input=round(chars / 4),
                           output=30 * len(batch), estimated=True))
    return calls


# ── Rough cost of one question before asking ──────────────────────────────────

def typical_question(answer_model: str, articles_chars: int = 40_000) -> list[Usage]:
    """A typical question with laws already in the cache: Haiku for the small steps, the chosen model for
    the answer. Used only for the preview; real counts replace it once questions are asked."""
    h = "claude-haiku-4-5"
    ctx = round(articles_chars / 4)
    return [
        Usage(h, "Sort the question", input=380, output=60, estimated=True),
        Usage(h, "Pick search words", input=520, output=70, estimated=True),
        Usage(h, "Choose articles", input=3_400, output=250, estimated=True),
        Usage(answer_model, "Write the answer", input=ctx + 1_100, output=1_800, estimated=True),
        Usage(h, "Check the answer", input=ctx + 1_500, output=180, estimated=True),
    ]
