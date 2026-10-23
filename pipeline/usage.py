"""The usage ledger kept in the visitor's browser: totals per day, per model and per step, since the last reset.

It is small on purpose (a few KB in the browser's local storage): daily totals for the current month,
one total for last month, per-model and per-step totals, the 5 most expensive questions, what the
law cache saved, and the monthly budget. The API key is never part of it.
"""

import base64
import datetime as dt
import json

STORAGE_KEY = "lextree_usage"       # localStorage key in the visitor's browser
MAX_BYTES = 3500
WARN_AT = 0.8
DEFAULT_BUDGET = 2.0      # euros per month

# a "row" is [calls, input, output, cache_read, cache_write, eur]


def _row():
    return [0, 0, 0, 0, 0, 0.0]


def _add(row, calls, s):
    row[0] += calls
    row[1] += s["input"]
    row[2] += s["output"]
    row[3] += s["cache_read"]
    row[4] += s["cache_write"]
    row[5] = round(row[5] + (s["eur"] or 0.0), 6)
    return row


def empty(today, budget=DEFAULT_BUDGET):
    return {"v": 1, "since": today.isoformat(), "month": today.strftime("%Y-%m"), "days": {}, "prev": None,
            "models": {}, "steps": {}, "all": _row(), "asked": 0, "free": 0, "saved": 0.0, "top": [],
            "budget": budget}


def roll(state, today):
    """New month: keep last month's total, start the daily list again."""
    month = today.strftime("%Y-%m")
    if state["month"] != month:
        spent = sum(r[5] for r in state["days"].values())
        state["prev"] = {"month": state["month"], "eur": round(spent, 6)}
        state["days"] = {}
        state["month"] = month
    return state


def record(state, calls, question, today, prices=None, usd_to_eur=None, saved=0.0, prefix=""):
    """Add one answered question: `calls` is its list of costs.Usage; `saved` is what the law cache saved."""
    from . import costs
    prices = prices or costs.PRICES
    usd_to_eur = usd_to_eur or costs.USD_TO_EUR
    roll(state, today)
    calls = list(calls)
    summary = costs.total(calls, prices, usd_to_eur)
    day = today.isoformat()[8:10]
    _add(state["days"].setdefault(day, _row()), summary["calls"], summary)
    _add(state["all"], summary["calls"], summary)
    for key, group in (("models", lambda u: prefix + u.model), ("steps", lambda u: u.step)):
        buckets = {}
        for u in calls:
            buckets.setdefault(group(u), []).append(u)
        for name, us in buckets.items():
            _add(state[key].setdefault(name, _row()), len(us), costs.total(us, prices, usd_to_eur))
    state["asked"] = state.get("asked", 0) + 1
    state["saved"] = round(state.get("saved", 0.0) + (saved or 0.0), 6)
    if summary["eur"]:
        top = state["top"] + [[round(summary["eur"], 6), (prefix + (question or ""))[:60]]]
        state["top"] = sorted(top, key=lambda t: -t[0])[:5]
    return state


def month_row(state, today):
    roll(state, today)
    row = _row()
    for r in state["days"].values():
        for i in range(6):
            row[i] += r[i]
    return row


def day_row(state, today):
    roll(state, today)
    return state["days"].get(today.isoformat()[8:10], _row())


def status(state, today):
    """How much of the monthly budget is used: level is ok, warn or over."""
    spent = month_row(state, today)[5]
    budget = state.get("budget") or 0
    share = spent / budget if budget > 0 else 0.0
    level = "over" if budget > 0 and spent >= budget else "warn" if share >= WARN_AT else "ok"
    return {"spent": spent, "budget": budget, "share": share, "level": level}


def avg_cost_per_question(state):
    """Average euros per AI-answered question since the last reset (a question can need several calls)."""
    asked = state.get("asked", 0)
    return state["all"][5] / asked if asked else None


def merge(state, extra, today):
    """Add the usage in `extra` (counted before the saved ledger was loaded) into `state`."""
    roll(state, today)
    roll(extra, today)
    for name in ("days", "models", "steps"):
        for k, r in extra[name].items():
            row = state[name].setdefault(k, _row())
            for i in range(6):
                row[i] = round(row[i] + r[i], 6) if i == 5 else row[i] + r[i]
    for i in range(6):
        state["all"][i] = round(state["all"][i] + extra["all"][i], 6) if i == 5 else state["all"][i] + extra["all"][i]
    state["asked"] = state.get("asked", 0) + extra.get("asked", 0)
    state["free"] += extra["free"]
    state["saved"] = round(state.get("saved", 0.0) + extra.get("saved", 0.0), 6)
    state["top"] = sorted(state["top"] + extra["top"], key=lambda t: -t[0])[:5]
    return state


def encode(state):
    """Storage-safe text (URL-safe base64 of compact JSON, no padding), trimmed to fit."""
    s = dict(state)
    while True:
        raw = base64.urlsafe_b64encode(json.dumps(s, separators=(",", ":")).encode()).decode().rstrip("=")
        if len(raw) <= MAX_BYTES:
            return raw
        if s["top"]:
            s = dict(s, top=s["top"][:-1])
        elif len(s["models"]) > 3 or len(s["steps"]) > 8:
            keep = sorted(s["models"].items(), key=lambda kv: -kv[1][5])[:3]
            steps = sorted(s["steps"].items(), key=lambda kv: -kv[1][5])[:8]
            s = dict(s, models=dict(keep), steps=dict(steps))
        else:
            oldest = sorted(s["days"])[0]
            s = dict(s, days={k: v for k, v in s["days"].items() if k != oldest})


def decode(text, today):
    """The ledger from the saved text, or a fresh one if it is missing or damaged."""
    try:
        text = text.strip().strip('"')
        s = json.loads(base64.urlsafe_b64decode((text + "=" * (-len(text) % 4)).encode()).decode())
        if s.get("v") != 1 or not isinstance(s.get("days"), dict) or len(s.get("all", [])) != 6:
            raise ValueError
        s.setdefault("asked", 0)
        s.setdefault("steps", {})
        s.setdefault("saved", 0.0)
        return roll(s, today)
    except Exception:
        return empty(today)
