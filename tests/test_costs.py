"""Tests for token counting, prices, the usage ledger, the metered client and demo recordings.

No API is called: a fake client returns replies with `usage` like the Anthropic SDK does.
"""

import datetime as dt
import json
from types import SimpleNamespace

from pipeline import costs, demo, usage
from pipeline.metering import Meter, MeteredClient, MeteredGemini, current_step

TODAY = dt.date(2026, 10, 23)


# ── fake Anthropic client ─────────────────────────────────────────────────────

def _reply(text, inp=100, out=20, read=0, write=0):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=inp, output_tokens=out,
                              cache_read_input_tokens=read, cache_creation_input_tokens=write))


class _FakeStream:
    def __init__(self, text):
        self.text_stream = iter([text[:5], text[5:]])
        self._text = text

    def get_final_message(self):
        return _reply(self._text, inp=5000, out=900)


class _FakeStreamManager:
    def __init__(self, text):
        self._s = _FakeStream(text)

    def __enter__(self):
        return self._s

    def __exit__(self, *exc):
        return False


class _FakeMessages:
    def __init__(self):
        self.kwargs = []

    def create(self, **kw):
        self.kwargs.append(kw)
        return _reply('{"ok": true}', inp=300, out=40)

    def stream(self, **kw):
        self.kwargs.append(kw)
        return _FakeStreamManager("Article 33 says notify within 72 hours.")


class _FakeAnthropic:
    def __init__(self):
        self.messages = _FakeMessages()
        self.models = SimpleNamespace(list=lambda: ["claude-opus-4-6"])


# functions named like the pipeline's, so the step label is found from the caller
def extract_keywords_and_year(client):
    return client.messages.create(model="claude-haiku-4-5", max_tokens=10, messages=[{"role": "user", "content": "q"}])


def answer_stream(client):
    with client.messages.stream(model="claude-opus-4-6", max_tokens=10,
                                messages=[{"role": "user", "content": "q"}]) as s:
        return "".join(s.text_stream)


def ingest_uploaded_file(client):
    def _summarise_batch():
        return client.messages.create(model="claude-haiku-4-5", max_tokens=10, messages=[])
    return _summarise_batch()


# ── prices ──────────────────────────────────────────────────────────────────────

def test_price_and_dated_model_names():
    u = costs.Usage("claude-opus-4-6-20260101", input=1_000_000, output=1_000_000)
    assert abs(costs.cost_eur(u, usd_to_eur=1.0) - 30.0) < 1e-9          # $5 in + $25 out
    assert costs.cost_eur(costs.Usage("unknown-model", input=10)) is None
    assert costs.total([costs.Usage("unknown-model", input=10)])["eur"] is None


def test_haiku_5_5_long_prompts_cost_more():
    short = costs.Usage("claude-haiku-5-5", input=90_000)
    long = costs.Usage("claude-haiku-5-5", input=110_000)
    assert costs.cost_eur(long) / costs.cost_eur(short) > 5 * 110 / 90 - 0.01


def test_cache_reads_are_cheaper():
    cold = costs.Usage("claude-opus-4-6", input=20, cache_write=10_000, output=500)
    warm = costs.Usage("claude-opus-4-6", input=20, cache_read=10_000, output=500)
    assert costs.cost_eur(warm) < costs.cost_eur(cold) / 2


def test_gemini_usage_counts_thinking_as_output():
    meta = SimpleNamespace(prompt_token_count=1200, cached_content_token_count=200,
                           candidates_token_count=300, thoughts_token_count=100)
    u = costs.from_gemini(meta, "gemini-3.5-flash-lite")
    assert (u.input, u.cache_read, u.output) == (1000, 200, 400)


def test_thinking_setting_per_model():
    assert costs.thinking_for("claude-opus-4-6") == {"type": "adaptive"}
    assert costs.thinking_for("claude-haiku-5-5") == {"type": "adaptive"}
    assert costs.thinking_for("claude-haiku-4-5") is None


def test_index_cost_estimate_for_an_old_cached_law():
    tree = {"title": "GDPR", "nodes": [{"title": f"Art {i}", "text": "x" * 2000} for i in range(30)]}
    calls = costs.index_usage_estimate(tree)
    assert len(calls) == 3 and all(c.step == "Index new laws" and c.estimated for c in calls)
    tree["index_usage"] = [costs.Usage("claude-haiku-4-5", "Index new laws", input=7, output=3).to_dict()]
    assert costs.index_usage_estimate(tree)[0].input == 7


def test_money_format():
    assert costs.eur(None) == "price unknown"
    assert costs.eur(0) == "€0"
    assert costs.eur(0.00004) == "< €0.0001"
    assert costs.eur(0.0031) == "€0.0031"
    assert costs.eur(0.123) == "€0.123"
    assert costs.eur(12.5) == "€12.50"


# ── metered client ─────────────────────────────────────────────────────────────

def test_metered_client_counts_calls_and_labels_steps():
    fake = _FakeAnthropic()
    client = MeteredClient(fake)
    mark = client.meter.mark()
    extract_keywords_and_year(client)
    text = answer_stream(client)
    ingest_uploaded_file(client)
    calls = client.meter.since(mark)
    assert text == "Article 33 says notify within 72 hours."
    assert [c.step for c in calls] == ["Pick search words", "Write the answer", "Index uploads"]
    assert (calls[1].model, calls[1].input, calls[1].output) == ("claude-opus-4-6", 5000, 900)
    assert client.models.list() == ["claude-opus-4-6"]                  # everything else passes through
    assert current_step() == "Other"


def test_metered_gemini_stream():
    chunks = [SimpleNamespace(text="Hello ", usage_metadata=None),
              SimpleNamespace(text="world", usage_metadata=SimpleNamespace(
                  prompt_token_count=50, candidates_token_count=10, thoughts_token_count=0,
                  cached_content_token_count=0))]
    inner = SimpleNamespace(models=SimpleNamespace(generate_content_stream=lambda **kw: iter(chunks)))
    meter = Meter()
    g = MeteredGemini(inner, meter)
    out = "".join(c.text for c in g.models.generate_content_stream(model="gemini-3.5-flash-lite", contents="q"))
    assert out == "Hello world"
    assert (meter.calls[0].input, meter.calls[0].output, meter.calls[0].step) == (50, 10, "Gemini (web search)")


# ── ledger ──────────────────────────────────────────────────────────────────────

def _calls(eur_scale=1):
    return [costs.Usage("claude-haiku-4-5", "Choose articles", input=3000 * eur_scale, output=200),
            costs.Usage("claude-opus-4-6", "Write the answer", input=10_000 * eur_scale, output=1500)]


def test_ledger_totals_steps_and_savings():
    led = usage.empty(TODAY)
    usage.record(led, _calls(), "What does GDPR Article 33 require?", TODAY, saved=0.02)
    usage.record(led, _calls(), "Replay", TODAY - dt.timedelta(days=1), prefix="replay · ")
    assert usage.month_row(led, TODAY)[0] == 4
    assert set(led["steps"]) == {"Choose articles", "Write the answer"}
    assert "replay · claude-opus-4-6" in led["models"]
    assert abs(led["saved"] - 0.02) < 1e-9
    assert led["steps"]["Write the answer"][5] > led["steps"]["Choose articles"][5]
    assert led["asked"] == 2 and led["top"][0][1] == "What does GDPR Article 33 require?"


def test_budget_levels():
    led = usage.empty(TODAY, budget=0.05)
    assert usage.status(led, TODAY)["level"] == "ok"
    usage.record(led, _calls(), "q", TODAY)                              # about €0.10
    assert usage.status(led, TODAY)["level"] == "over"
    led["budget"] = 0
    assert usage.status(led, TODAY)["level"] == "ok"                     # 0 = no limit


def test_ledger_round_trip_merge_and_damage():
    led = usage.empty(TODAY)
    for d in range(1, 24):
        usage.record(led, _calls(), "a long question " * 8, dt.date(2026, 10, d), saved=0.001)
    text = usage.encode(led)
    assert len(text) <= usage.MAX_BYTES
    back = usage.decode(text, TODAY)
    assert abs(back["all"][5] - led["all"][5]) < 1e-6
    early = usage.empty(TODAY)
    usage.record(early, _calls(), "before loading", TODAY)
    merged = usage.merge(back, early, TODAY)
    assert merged["asked"] == 24
    assert usage.decode("not base64!", TODAY)["all"] == [0, 0, 0, 0, 0, 0.0]
    assert "key" not in json.dumps(usage.empty(TODAY))


# ── demo recordings ────────────────────────────────────────────────────────────

def test_demo_record_save_and_load(tmp_path):
    rec = demo.make_record(
        question="When must a personal data breach be notified?", answer="Within 72 hours (Article 33).",
        references=[{"CELEX": "32016R0679", "Article": "Article 33"}],
        confidence={"score": 88, "level": "high", "coverage": 90, "grounding": 87},
        meta={"keywords": ["data breach"], "docs_found": 3, "selected": ["not kept"]},
        calls=_calls(), saved_eur=0.01, answer_model="claude-opus-4-6")
    assert "selected" not in rec["meta"]
    path = demo.save(rec, str(tmp_path))
    assert path.endswith("when-must-a-personal-data-breach-be-notified.json")
    (tmp_path / "broken.json").write_text("{not json")
    loaded = demo.load_all(str(tmp_path))
    assert len(loaded) == 1 and demo.is_valid(loaded[0])
    assert [u.step for u in demo.calls_of(loaded[0])] == ["Choose articles", "Write the answer"]
    assert "".join(demo.chunks(rec["answer"], 5)) == rec["answer"]
    assert "api_key" not in json.dumps(rec).lower()


def test_shipped_demo_examples_are_valid():
    for rec in demo.load_all():
        assert demo.is_valid(rec)
        assert costs.total(demo.calls_of(rec))["calls"] > 0
