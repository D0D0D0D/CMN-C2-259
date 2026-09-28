# CMN-C2-259 — Unit Tests: inner domain nodes

from __future__ import annotations

import json

from framework.schemas.agent_status import AgentStatus
from src.nodes.stagnation_detect_node import StagnationDetectNode
from src.nodes.scoring_node import ScoringNode
from src.nodes.priority_decision_node import PriorityDecisionNode

CFG = {
    "scoring": {
        "weights": {"phase": 0.35, "amount": 0.25, "recency": 0.20, "bant": 0.20},
        "phase_scores": {"proposal": 0.5, "negotiation": 0.75},
        "amount_ceiling": 100_000_000, "recency_horizon_days": 90,
    },
    "stagnation_days": 14,
    "routing_table": [
        {"min_score": 0.7, "stagnant": True, "priority": "P1-urgent"},
        {"min_score": 0.0, "stagnant": None, "priority": "P4-low"},
    ],
}


def test_stagnation_unpacks_payload_and_flags():
    payload = {
        "deals": [{"deal_id": "D1", "last_updated": "2026-06-01"}],
        "missing_map": {}, "input_context": {"_config": CFG, "today": "2026-07-03"},
    }
    out = StagnationDetectNode().execute({"user_input": json.dumps(payload)})
    assert out["status"] == AgentStatus.SUCCESS
    assert out["deals"][0]["_stagnant"] is True  # >14 days
    assert out["input_context"]["_config"] == CFG  # round-trips via JSON


def test_stagnation_bad_json_yields_empty():
    out = StagnationDetectNode().execute({"user_input": "not-json"})
    assert out["deals"] == []
    assert out["status"] == AgentStatus.SUCCESS


def test_scoring_adds_score_and_coverage():
    state = {
        "deals": [{"deal_id": "D1", "phase": "negotiation"}],
        "input_context": {"_config": CFG, "today": "2026-07-03"},
    }
    out = ScoringNode().execute(state)
    d = out["deals"][0]
    assert d["score"] is not None and d["coverage"] == 0.25


def test_priority_uses_routing_table():
    state = {
        "deals": [{"deal_id": "D1", "score": 0.9, "_stagnant": True}],
        "input_context": {"_config": CFG},
    }
    out = PriorityDecisionNode().execute(state)
    assert out["deals"][0]["priority"] == "P1-urgent"
