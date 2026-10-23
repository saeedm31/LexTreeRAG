"""
Which Claude endpoint the app talks to.

- LLM_PROVIDER=anthropic (default): Anthropic's API with the user's key (the public demo).
- LLM_PROVIDER=bedrock: Amazon Bedrock in the company's own AWS account. No key: the app's
  AWS role is allowed to call the models. Requests go to the Bedrock endpoint in AWS_REGION
  (default eu-north-1, Stockholm), so prompts and answers stay in that EU region.

Bedrock uses its own model IDs; the metered client translates the plain names the pipeline uses
(e.g. "claude-haiku-4-5") into Bedrock IDs, so the pipeline code is the same in both setups.
"""

from __future__ import annotations

import json
import os

from .metering import Meter, MeteredClient

PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
AWS_REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "eu-north-1"

# Plain model name -> Bedrock model ID (Bedrock's Messages-API endpoint, "bedrock-mantle").
# Claude Opus 4.6 / Sonnet 4.6 are not on that endpoint, so on AWS the answer models are the 5.5 family.
BEDROCK_MODEL_IDS = {
    "claude-haiku-4-5":  "anthropic.claude-haiku-4-5",
    "claude-haiku-5-5":  "anthropic.claude-haiku-5-5",
    "claude-sonnet-5-5": "anthropic.claude-sonnet-5-5",
    "claude-opus-5-5":   "anthropic.claude-opus-5-5",
}
BEDROCK_ANSWER_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-haiku-4-5"]


def is_bedrock() -> bool:
    return PROVIDER == "bedrock"


def model_map() -> dict:
    """Bedrock model IDs, with optional overrides from BEDROCK_MODEL_MAP (a JSON object)."""
    ids = dict(BEDROCK_MODEL_IDS)
    extra = os.getenv("BEDROCK_MODEL_MAP")
    if extra:
        ids.update(json.loads(extra))
    return ids


def answer_models() -> list[str]:
    """Models the answer can be written with in this setup (None = any model in costs.ANSWER_MODELS)."""
    if not is_bedrock():
        return None
    allowed = os.getenv("ANSWER_MODELS")
    return [m.strip() for m in allowed.split(",") if m.strip()] if allowed else list(BEDROCK_ANSWER_MODELS)


def default_answer_model() -> str:
    return os.getenv("DEFAULT_ANSWER_MODEL") or ("claude-opus-5-5" if is_bedrock() else "claude-opus-4-6")


def bedrock_client(meter: Meter | None = None) -> MeteredClient:
    """Claude on Amazon Bedrock, signed with the app's AWS credentials (task role on ECS)."""
    from anthropic import AnthropicBedrockMantle      # needs anthropic[bedrock] >= 1.0
    return MeteredClient(AnthropicBedrockMantle(aws_region=AWS_REGION), meter, model_map=model_map())
