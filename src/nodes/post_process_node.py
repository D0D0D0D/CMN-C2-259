"""AgentCore Platform v1.0"""

# post_process (FunctionNode): render the Markdown report separating the
# deterministic score from the LLM qualitative findings, flag low-coverage
# rows, and run the S-3 output gate before returning.

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import build_report


class PostProcessNode(FunctionNode):
    """Assemble the report and apply the S-3 output gate."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        # Pass through an upstream error unchanged (main may have propagated).
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        scored_deals = state.get("scored_deals", []) or []
        qualitative = state.get("qualitative", {}) or {}
        missing_map = state.get("missing_map", {}) or {}
        non_conformant = bool(state.get("non_conformant", False))
        cfg = (state.get("input_context", {}) or {}).get("_config", {}) or {}
        low_ratio = float(cfg.get("low_coverage_flag_ratio", 0.4))

        report = build_report(
            scored_deals, qualitative, missing_map, non_conformant, low_ratio
        )

        emit_trace_event(
            "deal_report_built",
            {"deal_count": len(scored_deals), "non_conformant": non_conformant},
            state,
        )

        return {
            "result": report,
            "output": report,
            "formatted_output": report,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_output(self, result: dict) -> dict:
        """S-3 hook: re-check the rendered report for PII / credentials before
        it leaves the agent (the LLM qualitative text is included in output)."""
        from shared.security.pii_detector import detect_pii

        report = ""
        if isinstance(result, dict):
            report = str(result.get("output") or result.get("result") or "")
        if report:
            hits = detect_pii(report)
            if hits:
                # Redact rather than block: mask detected spans, keep the report.
                masked = report
                for hit in hits:
                    span = hit.get("value") or hit.get("match")
                    if span:
                        masked = masked.replace(str(span), "[REDACTED]")
                if isinstance(result, dict):
                    result = {**result, "output": masked, "formatted_output": masked, "result": masked}
        return result
