"""AgentCore Platform v1.0"""

# Inner-graph node #3 (deterministic).
# Rule-based priority ranking via a config routing table (Financial Document Classification & Routing Agent style).

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import decide_priority


class PriorityDecisionNode(FunctionNode):
    """Assign a priority rank to each scored deal via the routing table."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        deals = state.get("deals", []) or []
        input_context = state.get("input_context", {}) or {}
        routing_table = (input_context.get("_config") or {}).get("routing_table", [])

        ranked = []
        for deal in deals:
            priority = decide_priority(
                deal.get("score"),
                deal.get("_stagnant"),
                routing_table,
            )
            ranked.append({**deal, "priority": priority})

        emit_trace_event(
            "deal_priority_decision",
            {"ranked_count": len(ranked)},
            state,
        )

        return {
            "deals": ranked,
            "status": AgentStatus.SUCCESS.value,
        }
