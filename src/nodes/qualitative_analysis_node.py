"""AgentCore Platform v1.0"""

# Inner-graph node #4 (LLM).
# Extracts qualitative risk signals from free-text `notes` that the
# deterministic score cannot capture, and proposes per-deal next actions.
# Only deals WITH notes are sent to the LLM, capped at max_deals.

from __future__ import annotations

import json

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets import current_secrets
from shared.utils.audit_logger import emit_trace_event

from src.services import llm_client

_SYSTEM_PROMPT = (
    "あなたは営業案件レビューの補佐AIです。案件の備考(notes)から、"
    "受注確度スコアには現れない定性的リスク(決裁者の渋り・競合の影・予算懸念・"
    "反応の停滞など)を読み取り、案件ごとに次に取るべき具体的アクションを1〜3点提案してください。"
    "推測が過ぎる断定は避け、根拠が薄い場合はその旨を添えてください。"
    "出力は必ず次のJSON形式のみ: "
    '{"items": [{"deal_id": "...", "risks": ["..."], "actions": ["..."]}]}'
)


class QualitativeAnalysisNode(FunctionNode):
    """LLM qualitative risk extraction over deals that have notes."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _get_llm(self, llm_cfg: dict):
        api_key = None
        key_name = llm_cfg.get("api_key_secret")
        if key_name:
            # require() raises MissingSecret when absent — surfaced as error.
            api_key = current_secrets().require(key_name)
        # Module-level call so tests can monkeypatch llm_client.build_llm
        # (nodes are re-created on compile(), so instance injection is lost).
        return llm_client.build_llm(llm_cfg, api_key)

    def execute(self, state: dict) -> dict:
        deals = state.get("deals", []) or []
        input_context = state.get("input_context", {}) or {}
        llm_cfg = (input_context.get("_config") or {}).get("llm", {})
        max_deals = int(llm_cfg.get("max_deals", 20))

        # Only deals that actually have notes are worth an LLM pass.
        candidates = [d for d in deals if str(d.get("notes") or "").strip()]
        candidates = candidates[:max_deals]

        if not candidates:
            return {"qualitative": {}, "status": AgentStatus.SUCCESS.value}

        llm = self._get_llm(llm_cfg)
        payload = [
            {"deal_id": d.get("deal_id"), "notes": d.get("notes")}
            for d in candidates
        ]
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

        emit_trace_event(
            "deal_qualitative_llm_call",
            {"analyzed_count": len(candidates), "model": llm_cfg.get("model")},
            state,
        )

        response = llm.complete(messages)
        qualitative, parse_ok = self._parse(response)
        if not parse_ok:
            # Audit the miss rather than silently dropping all analysis.
            emit_trace_event(
                "deal_qualitative_parse_failed",
                {"analyzed_count": len(candidates)},
                state,
            )

        return {"qualitative": qualitative, "status": AgentStatus.SUCCESS.value}

    @staticmethod
    def _coerce_list(value) -> list:
        """Coerce an LLM field to list[str]. A bare string becomes [string]
        (never a char array); other scalars are wrapped; None -> []."""
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v) for v in value]
        return [str(value)]

    @classmethod
    def _parse(cls, response: dict) -> tuple[dict, bool]:
        """Map the LLM JSON response into {deal_id: {risks, actions}}.

        Returns (mapping, parse_ok). parse_ok is False when the content was
        not valid JSON with an items list, so the caller can audit the miss
        instead of silently dropping all qualitative analysis.
        """
        content = ""
        if isinstance(response, dict):
            content = response.get("content", "") or ""
        else:
            content = str(response)
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            return {}, False
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return {}, False
        out: dict = {}
        for item in data["items"]:
            if not isinstance(item, dict):
                continue
            did = item.get("deal_id")
            if did is None:
                continue
            out[str(did)] = {
                "risks": cls._coerce_list(item.get("risks")),
                "actions": cls._coerce_list(item.get("actions")),
            }
        return out, True
