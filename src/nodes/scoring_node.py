"""AgentCore Platform v1.0"""

# Inner-graph node #2 (deterministic).
# Computes a missing-tolerant win-probability score with explicit coverage.

from __future__ import annotations

from datetime import date, datetime, timezone

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import score_deal


def _today() -> date:
    return datetime.now(timezone.utc).date()


class ScoringNode(FunctionNode):
    """Deterministic scoring over available factors only (coverage-aware)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        deals = state.get("deals", []) or []
        input_context = state.get("input_context", {}) or {}
        cfg = (input_context.get("_config") or {}).get("scoring", {})

        today_override = input_context.get("today")
        today = date.fromisoformat(today_override) if today_override else _today()

        scored = []
        for deal in deals:
            result = score_deal(deal, cfg, today)
            scored.append({**deal, **result})

        emit_trace_event(
            "deal_scoring",
            {"scored_count": len(scored)},
            state,
        )

        return {
            "deals": scored,
            "status": AgentStatus.SUCCESS.value,
        }
