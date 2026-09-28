"""Shared fixtures for live LLM smoke coverage.

The request and seed rows mirror `tests/integration/test_end_to_end.py`.
"""

from __future__ import annotations

import pytest


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
    "low_coverage_flag_ratio": 0.4,
}

DEALS = [
    {
        "deal_id": "D1",
        "deal_name": "A社更新",
        "account_name": "A社",
        "phase": "negotiation",
        "amount": 50000000,
        "last_updated": "2026-06-30",
        "bant_budget": "yes",
        "bant_authority": "yes",
        "bant_need": "yes",
        "bant_timeline": "no",
        "notes": "先方の決裁が渋い。競合B社が価格提示。",
    },
    {
        "deal_id": "D2",
        "deal_name": "C社新規",
        "account_name": "C社",
        "phase": "proposal",
        "amount": 10000000,
        "last_updated": "2026-05-01",
        "bant_budget": "no",
        "notes": "",
    },
]


class FakeRepository:
    def __init__(self, rows):
        self.rows = rows

    def list_deals(self, *, owner=None, phase=None):  # noqa: ARG002
        return list(self.rows)

    def close(self):
        pass


@pytest.fixture
def agent_config(monkeypatch) -> dict:
    """Graph config for the live-model check.

    The live checker injects a BaseLLM object at `config["llm"]`, while this
    template normally carries an LLM settings dict through the GraphNode JSON
    boundary. This fixture adapts only the live-check seam and keeps `src/`
    unchanged.
    """
    from shared.services.llm.base_llm import BaseLLM
    from src.graph.graph import DealScoringGraphNode
    from src.nodes.qualitative_analysis_node import QualitativeAnalysisNode
    from src.services import deal_repository_factory

    live_llm_client = {}
    monkeypatch.setattr(BaseLLM, "get", lambda self, key, default=None: default, raising=False)
    original_extract_input = DealScoringGraphNode.extract_input
    original_get_llm = QualitativeAnalysisNode._get_llm

    def extract_input_for_live_check(self, state):
        cfg = getattr(self, "agent_config", {}) or {}
        llm = cfg.get("llm") if isinstance(cfg, dict) else None
        if isinstance(llm, BaseLLM) or callable(getattr(llm, "complete", None)):
            live_llm_client["client"] = llm
            adapted_cfg = dict(cfg)
            adapted_cfg["llm"] = {"_live_llm_client": True, "max_deals": 20}
            previous_cfg = self.agent_config
            try:
                self.agent_config = adapted_cfg
                return original_extract_input(self, state)
            finally:
                self.agent_config = previous_cfg
        return original_extract_input(self, state)

    def get_llm_for_live_check(self, llm_cfg):
        if isinstance(llm_cfg, dict) and llm_cfg.get("_live_llm_client"):
            return live_llm_client["client"]
        if isinstance(llm_cfg, BaseLLM) or callable(getattr(llm_cfg, "complete", None)):
            return llm_cfg
        return original_get_llm(self, llm_cfg)

    monkeypatch.setattr(DealScoringGraphNode, "extract_input", extract_input_for_live_check)
    monkeypatch.setattr(QualitativeAnalysisNode, "_get_llm", get_llm_for_live_check)
    monkeypatch.setattr(
        deal_repository_factory,
        "create_deal_repository",
        lambda path, table: FakeRepository(DEALS),
    )
    return dict(CONFIG)


@pytest.fixture
def case_payload() -> str:
    """Integration `user_input` for the successful path."""
    return "run"


@pytest.fixture
def live_input_context() -> dict:
    """Integration input context for deterministic recency/stagnation scoring."""
    return {"today": "2026-07-03"}
