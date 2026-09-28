"""AgentCore Platform v1.0 — CMN-C2-259 outer graph."""

# Cat 2: AgentBaseGraph (L1 Base) + a GraphNode in the `main` slot wrapping
# the inner DealScoringWorkflowGraph. Fixed 5-node backbone:
#   START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
# add_edges()/route() are NOT overridden (framework default).
# See docs/02_design.md.

from __future__ import annotations

import json
from typing import ClassVar

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel

from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import State


class DealScoringGraphNode(GraphNode):
    """`main` slot — wraps the inner deal-scoring workflow graph.

    Data-passing contract (docs/02_design.md):
      extract_input : pack deals/missing_map/input_context into ONE JSON string
                      (GraphNode passes a single user_input string to the inner
                      graph; the inner entry node json.loads it back).
      merge_output  : map inner scored_deals/qualitative back into outer State.
    """

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def get_subgraph(self):
        from src.graph.domain_workflow_graph import DealScoringWorkflowGraph
        return DealScoringWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        # Rebuild input_context with the TRUSTED agent config (never the caller's
        # _config). Caller-supplied filter keys (owner/phase/today) are kept, but
        # _config is overwritten with the injected agent_config.
        caller_ctx = dict(state.get("input_context", {}) or {})
        caller_ctx["_config"] = getattr(self, "agent_config", {}) or {}
        payload = {
            "deals": state.get("deals", []),
            "missing_map": state.get("missing_map", {}),
            "input_context": caller_ctx,
        }
        # default=str keeps datetime/date safe; ensure_ascii=False keeps JP text.
        return json.dumps(payload, default=str, ensure_ascii=False)

    def merge_output(self, state: AgentState, sub_result: dict) -> dict:
        # Return ONLY changed keys (reducer double-counts full-state returns).
        delta = {
            "scored_deals": sub_result.get("scored_deals", []),
            "qualitative": sub_result.get("qualitative", {}),
            "status": sub_result.get("status"),
        }
        # Preserve inner observability so operators can trace which inner nodes
        # ran / why an inner error occurred. node_history uses an additive
        # reducer, so returning it appends (not full-state double count).
        if sub_result.get("trace_id"):
            delta["trace_id"] = sub_result["trace_id"]
        if sub_result.get("node_history"):
            delta["node_history"] = sub_result["node_history"]
        return delta

    def _parent_config(self) -> dict:
        # Forward the trusted agent config so inner nodes can read scoring/routing/llm.
        return getattr(self, "agent_config", {}) or {}


class DealScoringPipelinePriorityGraph(AgentBaseGraph):
    """CMN-C2-259 Enterprise Deal Scoring & Pipeline Priority Agent (Cat 2)."""

    @property
    def name(self) -> str:
        return "Enterprise Deal Scoring & Pipeline Priority Agent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects initialize + finalize
        # Inject the trusted runtime config (from config.yaml, held on self.config)
        # into the nodes at registration time. Nodes MUST use this — never a
        # caller-supplied input_context["_config"] — so a caller cannot swap the
        # target table / column_map (tenant isolation / SQL-injection surface).
        cfg = self.config or {}
        pre = PreProcessNode()
        pre.agent_config = cfg
        main = DealScoringGraphNode()
        main.agent_config = cfg
        post = PostProcessNode()
        post.agent_config = cfg
        self._nodes["pre_process"] = pre
        self._nodes["main"] = main
        self._nodes["post_process"] = post

    # add_edges() NOT overridden — backbone wiring belongs to the framework.
