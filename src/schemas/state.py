"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.

from __future__ import annotations

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """CMN-C2-259 Enterprise Deal Scoring & Pipeline Priority Agent state.

    Flat TypedDict extending AgentState. All values are primitives or
    JSON-serializable (list/dict of primitives) — msgpack safe.

    Shared fields (user_input, status, session_id, node_history, error_log,
    input_context, validated_input, result, formatted_output, ...) are
    inherited from AgentState.

    Data flow:
      pre_process  -> deals, missing_map, non_conformant
      main (inner) -> scored_deals, qualitative
      post_process -> output (Markdown report)
    """

    # ── pre_process outputs ──────────────────────────────────────────────
    # deals: Contract-conformant rows that passed validation (deal_id present).
    #        Each row holds only score factors + notes (+ display cols);
    #        PII contact columns are dropped before this list is built.
    deals: list[dict]
    # missing_map: deal_id -> list of missing/invalid factor descriptors.
    missing_map: dict
    # non_conformant: True when ALL fetched rows lack deal_id (schema drift,
    #        route (b)). main still runs but the inner graph returns 0 rows.
    non_conformant: bool

    # ── main (inner domain workflow) outputs ─────────────────────────────
    # scored_deals: rows enriched with score / priority / coverage / stagnation.
    scored_deals: list[dict]
    # qualitative: deal_id -> {"risks": [...], "actions": [...]} from the LLM.
    qualitative: dict
