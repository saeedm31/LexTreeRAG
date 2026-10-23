"""Tests for the AWS setup: Bedrock model names, the signed-in user, and usage kept in DynamoDB.

No AWS call is made: a fake client and a fake DynamoDB table stand in for the real ones.
"""

import base64
import datetime as dt
import json
from decimal import Decimal
from types import SimpleNamespace

from pipeline import costs, identity, llm_client, usage
from pipeline.metering import MeteredClient
from pipeline.usage_store import UsageStore

TODAY = dt.date(2026, 10, 23)


class _Msgs:
    def __init__(self):
        self.sent = []

    def create(self, **kw):
        self.sent.append(kw["model"])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="{}")],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                                     cache_read_input_tokens=0, cache_creation_input_tokens=0))


def navigate_tree(client):
    return client.messages.create(model="claude-haiku-4-5", max_tokens=5, messages=[])


def test_bedrock_ids_are_sent_but_usage_keeps_plain_names():
    msgs = _Msgs()
    client = MeteredClient(SimpleNamespace(messages=msgs), model_map=llm_client.model_map())
    navigate_tree(client)
    assert msgs.sent == ["anthropic.claude-haiku-4-5"]
    assert client.meter.calls[0].model == "claude-haiku-4-5"          # priced with the normal table
    assert client.meter.calls[0].step == "Choose articles"


def test_model_map_override_and_answer_models(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_MAP", json.dumps({"claude-haiku-4-5": "custom-id"}))
    assert llm_client.model_map()["claude-haiku-4-5"] == "custom-id"
    monkeypatch.setattr(llm_client, "PROVIDER", "bedrock")
    assert llm_client.answer_models()[0] == "claude-opus-5-5"
    assert all(m in costs.ANSWER_MODELS for m in llm_client.answer_models())
    assert all(m in llm_client.BEDROCK_MODEL_IDS for m in llm_client.answer_models())
    assert llm_client.default_answer_model() == "claude-opus-5-5"
    monkeypatch.setattr(llm_client, "PROVIDER", "anthropic")
    assert llm_client.answer_models() is None


def _jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'ES256'})}.{enc(claims)}.signature"


def test_user_from_load_balancer_headers(monkeypatch):
    monkeypatch.setenv("DEFAULT_TEAM", "legal")
    u = identity.current_user({"x-amzn-oidc-data": _jwt({"sub": "abc", "email": "ana@example.com"})})
    assert (u.id, u.email, u.team) == ("abc", "ana@example.com", "legal")
    u = identity.current_user({"x-amzn-oidc-data": _jwt({"sub": "x", "cognito:groups": ["compliance"]})})
    assert u.team == "compliance"
    assert identity.current_user({}).id == "anonymous"
    assert identity.current_user({"x-amzn-oidc-data": "garbage"}).id == "anonymous"


class _FakeTable:
    """Just enough of a DynamoDB table: get_item, put_item, and update_item with ADD."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[(Item["pk"], Item["sk"])] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues):
        item = self.items.setdefault((Key["pk"], Key["sk"]), dict(Key))
        for part in UpdateExpression.replace("ADD ", "").split(","):
            name, value = part.split()
            item[name] = item.get(name, 0) + ExpressionAttributeValues[value]


def test_usage_store_ledger_and_team_month():
    store = UsageStore(_FakeTable())
    assert store.load("u1", TODAY)["asked"] == 0
    led = usage.empty(TODAY)
    usage.record(led, [costs.Usage("claude-opus-5-5", "Write the answer", input=10_000, output=1_000)], "q", TODAY)
    store.save("u1", led)
    assert store.load("u1", TODAY)["asked"] == 1
    store.add_team("legal", "2026-10", 0.12, 5)
    store.add_team("legal", "2026-10", 0.08, 4)
    month = store.team_month("legal", "2026-10")
    assert abs(month["eur"] - 0.20) < 1e-9 and month["calls"] == 9 and month["questions"] == 2
    assert isinstance(store.table.items[("team#legal", "month#2026-10")]["eur"], Decimal)
    assert store.team_month("other", "2026-10") == {"eur": 0.0, "calls": 0, "questions": 0}


def test_price_factor(monkeypatch):
    u = costs.Usage("claude-opus-5-5", input=1_000_000)
    base = costs.cost_eur(u, usd_to_eur=1.0)
    monkeypatch.setattr(costs, "PRICE_FACTOR", 1.10)
    assert abs(costs.cost_eur(u, usd_to_eur=1.0) - base * 1.10) < 1e-9
