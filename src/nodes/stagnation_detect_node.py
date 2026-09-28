"""AgentCore Platform v1.0"""

# Inner-graph node #1 (deterministic).
# Also the entry point that unpacks the JSON payload the outer GraphNode
# passed as user_input (extract_input packs deals/missing_map/input_context
# into one JSON string). See docs/02_design.md — Cat2 GraphNode contract.

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import is_stagnant


def _today() -> date:
    # date.today() is avoided so behavior is deterministic under test injection;
    # callers may override via input_context["today"] (ISO date).
    return datetime.now(timezone.utc).date()


class StagnationDetectNode(FunctionNode):
    """Unpack the packed payload, then flag stagnant deals (deterministic)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        packed = state.get("user_input", "") or ""
        try:
            payload = json.loads(packed) if packed else {}
        except (ValueError, TypeError):
            payload = {}

        deals = payload.get("deals", []) or []
        missing_map = payload.get("missing_map", {}) or {}
        input_context = payload.get("input_context", {}) or {}
        cfg = (input_context.get("_config") or {})
        stagnation_days = int(cfg.get("stagnation_days", 14))

        today_override = input_context.get("today")
        today = date.fromisoformat(today_override) if today_override else _today()

        emit_trace_event(
            "deal_stagnation_scan",
            {"deal_count": len(deals), "stagnation_days": stagnation_days},
            state,
        )

        annotated = []
        for deal in deals:
            stag = is_stagnant(deal, today, stagnation_days)
            annotated.append({**deal, "_stagnant": stag})

        return {
            "deals": annotated,
            "missing_map": missing_map,
            "input_context": input_context,
            "status": AgentStatus.SUCCESS.value,
        }
