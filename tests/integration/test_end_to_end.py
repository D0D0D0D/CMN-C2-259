"""End-to-end invoke of the CMN-C2-259 agent with fake DB + fake LLM.

Nodes are re-created on compile(), so fakes are injected by monkeypatching
the repository / LLM factory functions (not node instances).
"""

from __future__ import annotations

import json

import pytest

from framework.schemas.invocation_context import InvocationContext, TrustLevel
from src.graph.graph import DealScoringPipelinePriorityGraph
from src.services import deal_repository_factory, llm_client


CONFIG = {
    "database": {"engine": "sqlite", "table": "deals"},
    "column_map": {},
    "scoring": {
        "weights": {"phase": 0.35, "amount": 0.25, "recency": 0.20, "bant": 0.20},
        "phase_scores": {"proposal": 0.5, "negotiation": 0.75, "won": 1.0, "lost": 0.0},
        "amount_ceiling": 100000000,
        "recency_horizon_days": 90,
    },
    "stagnation_days": 14,
    "routing_table": [
        {"min_score": 0.7, "stagnant": True, "priority": "P1-urgent"},
        {"min_score": 0.7, "stagnant": None, "priority": "P2-high"},
        {"min_score": 0.4, "stagnant": None, "priority": "P3-normal"},
        {"min_score": 0.0, "stagnant": None, "priority": "P4-low"},
    ],
    "llm": {"provider": "anthropic", "model": "claude-sonnet-5", "max_deals": 20},
    "low_coverage_flag_ratio": 0.4,
}

DEALS = [
    {
        "deal_id": "D1", "deal_name": "A社更新", "account_name": "A社",
        "phase": "negotiation", "amount": 50000000, "last_updated": "2026-06-30",
        "bant_budget": "yes", "bant_authority": "yes", "bant_need": "yes",
        "bant_timeline": "no", "notes": "先方の決裁が渋い。競合B社が価格提示。",
    },
    {
        "deal_id": "D2", "deal_name": "C社新規", "account_name": "C社",
        "phase": "proposal", "amount": 10000000, "last_updated": "2026-05-01",
        "bant_budget": "no", "notes": "",
    },
]


class FakeRepository:
    def __init__(self, rows):
        self.rows = rows

    def list_deals(self, *, owner=None, phase=None):
        return list(self.rows)

    def close(self):
        pass


class FakeLLM:
    def complete(self, messages):
        return {"content": json.dumps({"items": [
            {"deal_id": "D1", "risks": ["決裁停滞", "競合価格圧力"],
             "actions": ["決裁者へ直接アプローチ", "価格対抗の準備"]},
        ]})}


def _ctx():
    return InvocationContext(
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        caller_id="test",
    )


@pytest.fixture
def wired(monkeypatch):
    """Patch the factory seams so no real DB/LLM/secret is needed."""
    def _use(rows):
        monkeypatch.setattr(
            deal_repository_factory,
            "create_deal_repository",
            lambda path, table: FakeRepository(rows),
        )
        monkeypatch.setattr(llm_client, "build_llm", lambda cfg, key: FakeLLM())
        # no api_key_secret / path_secret_key => secrets are never required
        cfg = {k: v for k, v in CONFIG.items()}
        cfg["database"] = {**CONFIG["database"]}  # no path_secret_key
        cfg["llm"] = {k: v for k, v in CONFIG["llm"].items()}  # no api_key_secret
        return cfg
    return _use


def test_end_to_end_two_deals(wired):
    cfg = wired(DEALS)
    graph = DealScoringPipelinePriorityGraph(config=cfg)
    result = graph.invoke(
        user_input="run",
        session_id="s1",
        ctx=_ctx(),
        input_context={"_config": cfg, "today": "2026-07-03"},
    )
    assert result.get("status") == "success"
    out = result.get("output") or result.get("formatted_output") or result.get("result")
    assert out and "商談スコアリング" in out
    assert "A社" in out
    assert "決裁停滞" in out or "競合" in out


def test_non_conformant_all_missing_deal_id(wired):
    cfg = wired([{"deal_name": "x", "phase": "proposal"}])  # no deal_id
    graph = DealScoringPipelinePriorityGraph(config=cfg)
    result = graph.invoke(
        user_input="run", session_id="s2", ctx=_ctx(),
        input_context={"_config": cfg, "today": "2026-07-03"},
    )
    assert result.get("status") == "success"
    out = result.get("output") or result.get("result") or ""
    assert "Contract 非準拠" in out
