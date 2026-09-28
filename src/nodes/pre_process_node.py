"""AgentCore Platform v1.0"""

# pre_process (FunctionNode): fetch deals from the common sales DB and prepare
# them for scoring. Order is FIXED (docs/02_design.md):
#   fetch -> column_map -> S-2 PII drop -> contract validate -> build outputs
# The deals-fetch also threads the agent config into input_context["_config"]
# so inner nodes (which only get user_input+ctx) can read scoring/routing/llm.

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets import current_secrets
from shared.utils.audit_logger import emit_trace_event

from src.services import deal_repository_factory
from src.services.service import (
    apply_column_map,
    project_kept_columns,
    validate_row,
)


class PreProcessNode(FunctionNode):
    """Fetch, sanitize and contract-validate deals before scoring."""

    # S-1: reading the sales DB is a privileged operation; require a verified
    # external caller.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _get_repository(self, db_cfg: dict):
        database_path = None
        key = db_cfg.get("path_secret_key") or db_cfg.get("dsn_secret_key")
        if key:
            database_path = current_secrets().require(key)
        return deal_repository_factory.create_deal_repository(
            database_path,
            table=db_cfg.get("table", "deals"),
        )

    def execute(self, state: dict) -> dict:
        # SECURITY: read config from the injected TRUSTED agent_config only
        # (set at register_nodes from config.yaml) — NOT from a caller-supplied
        # input_context["_config"], which a caller could use to swap the target
        # table / column_map (tenant isolation / SQL-injection surface).
        cfg = getattr(self, "agent_config", {}) or {}
        db_cfg = cfg.get("database", {}) or {}
        column_map = cfg.get("column_map", {}) or {}
        table = db_cfg.get("table", "deals")

        # Keep only the caller's filter keys; overwrite _config with trusted cfg
        # so the inner graph downstream also uses the trusted config.
        caller = dict(state.get("input_context", {}) or {})
        caller.pop("_config", None)
        input_context = {**caller, "_config": cfg}

        # (1) fetch
        try:
            repository = self._get_repository(db_cfg)
            try:
                raw_rows = repository.list_deals(
                    owner=input_context.get("owner"),
                    phase=input_context.get("phase"),
                )
            finally:
                repository.close()
        except Exception as exc:  # DB connect/fetch failure = (C) error branch
            emit_trace_event("deals_fetch_error", {"error": str(exc)[:200]}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: deals fetch failed: {exc}"],
            }

        emit_trace_event("deals_fetch", {"row_count": len(raw_rows), "table": table}, state)

        # (2) column_map  (3) PII drop  (4) contract validate  (5) build
        deals: list[dict] = []
        missing_map: dict = {}
        total = len(raw_rows)
        skipped_no_id = 0
        for raw in raw_rows:
            mapped = apply_column_map(raw, column_map)     # (2)
            kept = project_kept_columns(mapped)            # (3) drop PII contact cols
            issues = validate_row(kept)                    # (4)
            if "deal_id_missing" in issues:
                skipped_no_id += 1
                continue                                   # skip unidentifiable row
            deal_id = str(kept["deal_id"])
            if issues:
                missing_map[deal_id] = issues
            deals.append(kept)

        # empty-deals branches (docs/02_design.md)
        non_conformant = total > 0 and skipped_no_id == total  # (A) all rows lack deal_id
        emit_trace_event(
            "deals_prepared",
            {"kept": len(deals), "skipped_no_id": skipped_no_id, "non_conformant": non_conformant},
            state,
        )

        return {
            "deals": deals,
            "missing_map": missing_map,
            "non_conformant": non_conformant,
            "input_context": input_context,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        """S-2 hook: scan EACH fetched deal row (all string fields, not just
        notes) for residual PII and mask it before the rows flow into the inner
        graph / LLM / output.

        Contact PII columns (email/phone) are already dropped in execute() via
        project_kept_columns(); this hook is the second line of defense for PII
        that slipped into free-text fields such as notes/deal_name. Detected
        spans are masked in place (not merely flagged) so no raw PII reaches the
        LLM prompt or the report.
        """
        from shared.security.pii_detector import detect_pii

        deals = state.get("deals", []) or []
        flagged = 0
        for deal in deals:
            row_flagged = False
            for key, value in list(deal.items()):
                if key.startswith("_") or not isinstance(value, str) or not value:
                    continue
                hits = detect_pii(value)
                if not hits:
                    continue
                masked = value
                for hit in hits:
                    span = hit.get("value") or hit.get("match")
                    if span:
                        masked = masked.replace(str(span), "[REDACTED]")
                deal[key] = masked
                row_flagged = True
            if row_flagged:
                deal["_pii_flag"] = True
                flagged += 1
        if flagged:
            emit_trace_event("deals_pii_masked", {"flagged": flagged}, state)
        return state
