"""
Usage kept on the server (Amazon DynamoDB) instead of the browser, for the AWS deployment.

One table, two kinds of items:
- pk "user#<id>", sk "ledger": that person's ledger (same format as pipeline.usage, as JSON).
- pk "team#<team>", sk "month#YYYY-MM": the team's spend this month, updated with atomic
  counters, so several users can add to it at the same time. The team budget is checked
  against it before every question.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

from . import usage


class UsageStore:
    def __init__(self, table):
        self.table = table

    @classmethod
    def from_env(cls) -> "UsageStore | None":
        name = os.getenv("USAGE_TABLE")
        if not name:
            return None
        import boto3
        return cls(boto3.resource("dynamodb").Table(name))

    # ── personal ledger ──
    def load(self, user_id: str, today) -> dict:
        item = self.table.get_item(Key={"pk": f"user#{user_id}", "sk": "ledger"}).get("Item")
        if not item:
            return usage.empty(today)
        try:
            return usage.roll(json.loads(item["data"]), today)
        except (KeyError, ValueError):
            return usage.empty(today)

    def save(self, user_id: str, ledger: dict) -> None:
        self.table.put_item(Item={"pk": f"user#{user_id}", "sk": "ledger",
                                  "data": json.dumps(ledger, separators=(",", ":"))})

    # ── team month ──
    def add_team(self, team: str, month: str, eur: float, calls: int, questions: int = 1) -> None:
        self.table.update_item(
            Key={"pk": f"team#{team}", "sk": f"month#{month}"},
            UpdateExpression="ADD eur :e, calls :c, questions :q",
            ExpressionAttributeValues={":e": Decimal(str(round(eur or 0.0, 6))), ":c": int(calls),
                                       ":q": int(questions)})

    def team_month(self, team: str, month: str) -> dict:
        item = self.table.get_item(Key={"pk": f"team#{team}", "sk": f"month#{month}"}).get("Item") or {}
        return {"eur": float(item.get("eur", 0)), "calls": int(item.get("calls", 0)),
                "questions": int(item.get("questions", 0))}


def team_budget() -> float:
    """Monthly budget per team in euros (TEAM_MONTHLY_BUDGET_EUR); 0 means no team limit."""
    return float(os.getenv("TEAM_MONTHLY_BUDGET_EUR", "0") or 0)
