"""AgentCore Platform v1.0 — CMN-C2-259 HTTP entry point.

Entry points are adapters only — no business logic here. For platform-level
routing, AgentGateway calls agent.invoke() directly.

Entry authentication is fail-closed and implemented by entry_adapter.auth:
  - a verified middleware trust (request.state.trust_level) is used as-is;
  - otherwise INVOKE_AUTH_TOKEN Bearer auth promotes ANONYMOUS →
    VERIFIED_EXTERNAL (_resolve_trust raises 401 on token mismatch);
  - an unauthenticated caller (no verified middleware trust AND no token
    promotion) is rejected with 401. The old fallback that defaulted such
    callers to ANONYMOUS and passed them straight into InvocationContext was
    fail-open (S-1 bypass) and is removed.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory

from src.entry_adapter.auth import _require_verified_trust, _resolve_trust
from src.graph.graph import DealScoringPipelinePriorityGraph

app = FastAPI(title="Enterprise Deal Scoring & Pipeline Priority Agent")

_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _load_runtime_config() -> dict:
    """Load the required trusted runtime config from config/config.yaml.

    PyYAML is a required project dependency. The trusted config is injected into
    the graph at construction so PreProcessNode/GraphNode read scoring/routing/
    database from config.yaml — NEVER from caller input_context.
    """
    try:
        doc = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(
            f"Unable to load required runtime config: {_RUNTIME_CONFIG_PATH}"
        ) from exc
    if not isinstance(doc, dict):
        raise RuntimeError(
            f"Required runtime config must contain a YAML mapping: {_RUNTIME_CONFIG_PATH}"
        )
    return doc


agent = DealScoringPipelinePriorityGraph(config=_load_runtime_config())
agent.compile()
# Replace namespace/agent_name to match the agent's manifest values.
agent.provision_secrets(secrets_factory(namespace="cmn", agent_name="cmn_c2_259"))


def _ctx(request: Request, session_id: str) -> InvocationContext:
    """Build the InvocationContext AFTER entry authentication.

    Scope note: the entry_adapter part also exposes a tenant/org scope contract,
    but this template has NO scope model. The deals table is reached via the
    per-deployment `DEALS_DB_PATH` secret; tenant isolation is a DEPLOYMENT
    boundary (the path points at one company's deals store), NOT a
    per-request scope key. There is no code path that reads request tenant/org
    scope. Enforcing authenticate_scope() would gate on a scope nothing consumes
    (STG-default theater; a "default" fallback would silently match everything),
    so only the auth core (_resolve_trust / _require_verified_trust) is wired.
    See config/config.yaml `database.path_secret_key`.
    """
    trust, token_authenticated = _resolve_trust(request)
    # Unauthenticated = trust is still ANONYMOUS after resolution and no token
    # promotion. Test the trust VALUE, not attribute presence — a middleware
    # that explicitly stamps ANONYMOUS must still be treated as unauthenticated
    # (→ 401), not silently accepted as "verified because the attr exists".
    if trust is TrustLevel.ANONYMOUS and not token_authenticated:
        raise HTTPException(status_code=401, detail="Authentication is required.")
    _require_verified_trust(trust)
    return InvocationContext(
        session_id=session_id or str(uuid4()),
        caller_trust_level=trust,
        caller_id=getattr(request.state, "caller_id", ""),
    )


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    input_context: dict = {}


# Public input_context contract (docs/07_operation_guide.md): only these
# caller-supplied filter keys are honoured. Everything else the caller sends —
# `today` (would let a caller move the clock to fake recency/stagnation and thus
# reorder pipeline priority), `_config` (would let a caller swap the target
# DB/table/scoring/routing), or any unknown key — is stripped at the entry
# boundary. The trusted `_config` is (re)injected downstream from config.yaml by
# PreProcessNode/GraphNode; `today` has no public override.
_ALLOWED_INPUT_CONTEXT_KEYS = frozenset({"owner", "phase"})


def _filtered_input_context(raw: dict) -> dict:
    """Keep only the public filter keys; drop caller-supplied control keys."""
    return {k: v for k, v in (raw or {}).items() if k in _ALLOWED_INPUT_CONTEXT_KEYS}


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    ctx = _ctx(request, req.session_id)
    with bound_secrets(agent._secrets_provider):
        return agent.invoke(
            req.input,
            session_id=ctx.session_id,
            ctx=ctx,
            input_context=_filtered_input_context(req.input_context),
        )


@app.get("/health")
def health():
    return {"status": "ok", "agent": "cmn_c2_259"}
