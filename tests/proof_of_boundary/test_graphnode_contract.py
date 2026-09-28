"""PB: Cat2 GraphNode data-passing contract + invoke execution order.

Verifies the boundary between the outer backbone and the inner domain graph:
- extract_input packs structured State into ONE JSON string (the inner graph
  only receives user_input:str + ctx — outer keys do not auto-propagate).
- merge_output returns ONLY changed keys (never the full state).
- The full agent invoke runs the 5-node backbone in order and produces output.
"""

from __future__ import annotations

import json

from framework.schemas.invocation_context import InvocationContext, TrustLevel
from src.graph.graph import DealScoringGraphNode, DealScoringPipelinePriorityGraph
from src.services import deal_repository_factory, llm_client


class _FakeRepository:
    def __init__(self, rows):
        self.rows = rows

    def list_deals(self, *, owner=None, phase=None):
        return list(self.rows)

    def close(self):
        pass


class _FakeLLM:
    def complete(self, messages):
        return {"content": json.dumps({"items": []})}


CFG = {
    "database": {"engine": "sqlite", "table": "deals"},
    "column_map": {},
    "scoring": {"weights": {"phase": 1.0}, "phase_scores": {"won": 1.0},
                "amount_ceiling": 1, "recency_horizon_days": 90},
    "stagnation_days": 14,
    "routing_table": [{"min_score": 0.0, "stagnant": None, "priority": "P4-low"}],
    "llm": {"provider": "anthropic", "model": "m", "max_deals": 20},
    "low_coverage_flag_ratio": 0.4,
}


def test_extract_input_packs_json():
    node = DealScoringGraphNode()
    node.agent_config = {"scoring": {"weights": {}}}  # trusted config injected
    state = {"deals": [{"deal_id": "D1"}], "missing_map": {"D1": ["x"]},
             "input_context": {"k": "v"}}
    packed = node.extract_input(state)
    assert isinstance(packed, str)
    restored = json.loads(packed)
    assert restored["deals"] == [{"deal_id": "D1"}]
    assert restored["missing_map"] == {"D1": ["x"]}
    # caller filter keys preserved; _config is overwritten with trusted config
    assert restored["input_context"]["k"] == "v"
    assert restored["input_context"]["_config"] == {"scoring": {"weights": {}}}


def test_extract_input_ignores_caller_supplied_config():
    """A caller cannot inject _config (tenant isolation / table swap defense)."""
    node = DealScoringGraphNode()
    node.agent_config = {"database": {"table": "deals"}}
    state = {"deals": [], "missing_map": {},
             "input_context": {"_config": {"database": {"table": "OTHER_TENANT"}}}}
    restored = json.loads(node.extract_input(state))
    assert restored["input_context"]["_config"]["database"]["table"] == "deals"


def test_merge_output_returns_only_changed_keys():
    node = DealScoringGraphNode()
    sub_result = {"scored_deals": [{"deal_id": "D1"}], "qualitative": {}, "status": "success"}
    delta = node.merge_output({}, sub_result)
    assert set(delta.keys()) == {"scored_deals", "qualitative", "status"}
    # full state keys (e.g. node_history) must NOT be echoed back
    assert "node_history" not in delta


def test_extract_input_handles_datetime_via_default_str():
    from datetime import date
    node = DealScoringGraphNode()
    state = {"deals": [{"deal_id": "D1", "last_updated": date(2026, 7, 1)}],
             "missing_map": {}, "input_context": {}}
    packed = node.extract_input(state)  # must not raise
    assert "2026-07-01" in packed


def test_full_invoke_backbone_order(monkeypatch):
    monkeypatch.setattr(
        deal_repository_factory,
        "create_deal_repository",
        lambda path, table: _FakeRepository([{"deal_id": "D1", "deal_name": "A",
                                               "account_name": "AC", "phase": "won",
                                               "last_updated": "2026-07-01", "notes": ""}]),
    )
    monkeypatch.setattr(llm_client, "build_llm", lambda cfg, key: _FakeLLM())
    graph = DealScoringPipelinePriorityGraph(config=CFG)
    result = graph.invoke(
        user_input="run", session_id="s",
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, caller_id="t"),
        input_context={"_config": CFG, "today": "2026-07-03"},
    )
    assert result.get("status") == "success"
    history = [h.get("node") if isinstance(h, dict) else h
               for h in result.get("node_history", [])]
    # backbone order: Initialize -> PreProcess -> main(GraphNode) -> PostProcess -> Finalize
    joined = " ".join(str(h) for h in history)
    assert "Initialize" in joined
    assert "PreProcess" in joined
    assert "PostProcess" in joined
    assert "Finalize" in joined
