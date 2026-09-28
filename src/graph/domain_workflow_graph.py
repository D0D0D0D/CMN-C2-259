"""AgentCore Platform v1.0"""

# Inner graph for CMN-C2-259 (Cat 2).
# Instantiated by DealScoringGraphNode.get_subgraph() in graph.py.
# Linear topology: stagnation_detect -> scoring -> priority_decision -> qualitative
# The first node unpacks the JSON payload the outer GraphNode packed into
# user_input (see docs/02_design.md — Cat2 GraphNode contract).

from __future__ import annotations

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus

from src.nodes.priority_decision_node import PriorityDecisionNode
from src.nodes.qualitative_analysis_node import QualitativeAnalysisNode
from src.nodes.scoring_node import ScoringNode
from src.nodes.stagnation_detect_node import StagnationDetectNode
from src.schemas.state import State


class DealScoringWorkflowGraph(BaseGraph):
    """Inner domain workflow: deterministic scoring + LLM qualitative analysis."""

    @property
    def name(self) -> str:
        return "DealScoringWorkflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory inner config — scoring/routing/llm config is threaded
        # in via input_context["_config"] and validated by the nodes.
        pass

    def register_nodes(self) -> None:
        # No super() — BaseGraph.register_nodes() is abstract.
        # Do NOT register initialize/finalize (outer backbone concern).
        self._nodes["stagnation_detect"] = StagnationDetectNode()
        self._nodes["scoring"] = ScoringNode()
        self._nodes["priority_decision"] = PriorityDecisionNode()
        self._nodes["qualitative"] = QualitativeAnalysisNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "stagnation_detect")
        self._sg.add_edge("stagnation_detect", "scoring")
        self._sg.add_edge("scoring", "priority_decision")
        self._sg.add_edge("priority_decision", "qualitative")
        self._sg.add_edge("qualitative", END)

    def route(self, state: AgentState) -> str:
        # Linear topology — route() is required by the ABC but never called
        # unless add_conditional_edges() references it.
        return END if state.get("status") == AgentStatus.ERROR.value else "scoring"

    def get_output(self, state: AgentState) -> dict:
        """Shape sub_result for the outer GraphNode.merge_output()."""
        return {
            "scored_deals": state.get("deals", []),
            "qualitative": state.get("qualitative", {}),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
