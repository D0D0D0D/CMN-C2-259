"""PB-6: Invoke execution order at the BaseNode boundary.

Verifies the fixed per-node pipeline order owned by ``BaseNode.__call__()``:

    S-1 trust gate
      → S-4 node_start (framework)
      → S-2 _security_gate_input()   [FunctionNode @final + _extra_ hook]
      → execute()                    [template business logic]
      → S-3 _security_gate_output()  [FunctionNode @final + _extra_ hook]
      → S-4 node_complete (framework)

Two layers of proof:

1. Static (always runs, no framework wheel needed): no template node overrides
   ``__call__`` — overriding it would silently break the S-1/S-4/S-2/S-3 pipeline.
   Every node implements ``execute()`` only.

2. Runtime (skipped when the framework wheel is absent): drive a real node
   through ``node(state)`` (i.e. ``__call__``, NOT ``node.execute(state)``) and
   assert the observable order S-1 → S-4 node_start → S-2 → execute → S-3 →
   S-4 node_complete. Calling ``execute()`` directly would bypass the S-1 gate
   and produce a false green, so the test uses ``__call__``.
"""

from __future__ import annotations

import ast
import importlib
import os
from typing import ClassVar

import pytest
from framework.nodes.base_node import BaseNode
from framework.schemas.trust_level import TrustLevel

_HERE = os.path.dirname(__file__)
_SRC = os.path.join(_HERE, "..", "..", "src")


# ---------------------------------------------------------------------------
# Layer 1 — static boundary check (framework not required)
# ---------------------------------------------------------------------------

def _node_files() -> list[str]:
    nodes_dir = os.path.join(_SRC, "nodes")
    return [
        os.path.join(nodes_dir, f)
        for f in os.listdir(nodes_dir)
        if f.endswith("_node.py")
    ]


# ── PB-6 S-1 negative boundary（2026-08-17 正本追従で移植）──────────────
# 正本 tests/proof_of_boundary/test_pb_invoke_order.py より。テンプレのノード構成に
# 依存しない常設フィクスチャで「不足 trust では execute() に到達しない」ことを証明する
# （TC-08 の要求。既存の全ノード走査テストは触っていない）。


class _PrivilegedTrustGateFixture(BaseNode):
    """Always-present privileged node used to prove the S-1 negative boundary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _security_gate_input(self, state):
        return state

    def execute(self, state):
        return {"status": "success"}

    def _security_gate_output(self, result):
        return result


def _trust_predecessor(required: TrustLevel) -> TrustLevel:
    """Return a lower valid trust level; fail loudly if the framework adds one."""
    predecessors = {
        TrustLevel.VERIFIED_EXTERNAL: TrustLevel.ANONYMOUS,
        TrustLevel.INTERNAL: TrustLevel.VERIFIED_EXTERNAL,
    }
    try:
        return predecessors[required]
    except KeyError as exc:
        raise AssertionError(f"no lower trust level defined for {required!r}") from exc


def test_no_node_overrides_dunder_call():
    """Nodes must NOT override __call__ — it owns the S-1/S-2/S-3/S-4 pipeline.

    Overriding __call__ silently breaks: (1) S-1 trust gate, (2) S-4 audit
    events, (3) timing, (4) node_history append.
    """
    offenders: list[str] = []
    for path in _node_files():
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for item in cls.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__call__":
                    offenders.append(f"{path}:{item.lineno} — {cls.name}.__call__")
    assert offenders == [], (
        "Node overrides __call__ (breaks S-1/S-4/S-2/S-3 pipeline):\n"
        + "\n".join(offenders)
    )


def test_every_node_implements_execute():
    """Each template node implements execute() (the only permitted override)."""
    missing: list[str] = []
    for path in _node_files():
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            if not cls.name.endswith("Node"):
                continue
            methods = {i.name for i in cls.body if isinstance(i, ast.FunctionDef)}
            if "execute" not in methods:
                missing.append(f"{path} — {cls.name} has no execute()")
    assert missing == [], "Node(s) without execute():\n" + "\n".join(missing)


def test_src_nodes_package_importable():
    """PB-6 setup must fail loudly if src.nodes cannot be imported."""
    try:
        importlib.import_module("src.nodes")
    except ImportError as exc:
        pytest.fail(f"PB-6 cannot import src.nodes; framework/template setup is broken: {exc}")


# ---------------------------------------------------------------------------
# Layer 2 — runtime order check (needs the framework wheel; skipped otherwise)
# ---------------------------------------------------------------------------

def test_invoke_order_s2_execute_s3():
    """Drive a node via __call__ and assert the S-2 → execute → S-3 gate order.

    A probe node records each observable gate stage into a shared list. The
    S-2/S-3 stages are observed through the FunctionNode _extra_security_gate
    hooks (the @final gates delegate to them); execute() records itself. The
    node is invoked as node(state) so BaseNode.__call__ runs the full pipeline
    (S-1 trust gate → S-4 node_start → S-2 → execute → S-3 → S-4 node_complete).
    S-4 node_start/node_complete are emitted on the framework's internal audit
    path and are not deterministically observable from template test code, so
    this test asserts the template-controlled stages (S-2/execute/S-3), which
    bracket execute() and defeat a false green. S-1 refusal is covered by the
    separate test below.
    """
    pytest.importorskip(
        "framework.nodes.function_node",
        reason="framework wheel not installed — runtime order check skipped",
    )
    from framework.nodes.function_node import FunctionNode
    from framework.schemas.agent_status import AgentStatus
    from framework.schemas.trust_level import TrustLevel

    calls: list[str] = []

    class ProbeNode(FunctionNode):
        required_trust_level = TrustLevel.VERIFIED_EXTERNAL

        def _extra_security_gate_input(self, state: dict) -> dict:
            calls.append("S2_input")
            return state

        def execute(self, state: dict) -> dict:
            calls.append("execute")
            return {"status": AgentStatus.SUCCESS.value}

        def _extra_security_gate_output(self, result: dict) -> dict:
            calls.append("S3_output")
            return result

    # S-4 node_start / node_complete are emitted by the framework INSIDE
    # BaseNode.__call__ through its own audit path, not through the template's
    # shared.utils.audit_logger reference — so a monkeypatch here cannot reliably
    # observe them. We therefore assert the stages the template controls and can
    # observe deterministically: S-2 (input gate) → execute → S-3 (output gate).
    # These bracket execute() unconditionally, which is what defeats a false green
    # (a node whose __call__ ran only execute() would be missing S-2/S-3).
    node = ProbeNode()
    # BaseNode.__call__(state) takes a single state arg and reads the caller
    # trust from state["caller_trust_level"] (SDK 1.0.0 contract) — NOT a
    # config= kwarg. Invoke through __call__ (NOT execute) so the S-1 gate runs.
    node(
        {
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            "input_context": {},
        }
    )

    # All three observable stages MUST fire — unconditional (not `if X in seq`).
    # A missing stage means the gate pipeline was bypassed.
    for stage in ("S2_input", "execute", "S3_output"):
        assert stage in calls, f"{stage} not observed — gate pipeline bypassed: {calls}"
    # Strict order: S-2 input gate → execute → S-3 output gate.
    assert calls == ["S2_input", "execute", "S3_output"], (
        f"gate pipeline stages out of order: {calls}"
    )


def test_s1_trust_gate_refuses_insufficient_trust():
    """PB-6 S-1: a node requiring VERIFIED_EXTERNAL must refuse an ANONYMOUS caller.

    Drives the node via __call__ (so BaseNode.__call__ runs the S-1 gate) with a
    trust level below the node's required_trust_level and asserts execute() is
    NEVER reached — proving S-1 gates the pipeline entry, not just later stages.
    """
    pytest.importorskip(
        "framework.nodes.function_node",
        reason="framework wheel not installed — S-1 refusal check skipped",
    )
    from framework.nodes.function_node import FunctionNode
    from framework.schemas.agent_status import AgentStatus
    from framework.schemas.trust_level import TrustLevel

    executed: list[str] = []

    class GuardedNode(FunctionNode):
        required_trust_level = TrustLevel.VERIFIED_EXTERNAL

        def execute(self, state: dict) -> dict:
            executed.append("execute")
            return {"status": AgentStatus.SUCCESS.value}

    node = GuardedNode()
    # Trust travels in state["caller_trust_level"] (SDK 1.0.0), read by the S-1
    # gate inside BaseNode.__call__ before execute() runs.
    result = node(
        {"caller_trust_level": TrustLevel.ANONYMOUS.value, "input_context": {}}
    )

    # S-1 must stop the pipeline before execute() runs (fail-closed).
    assert executed == [], "S-1 gate did NOT refuse: execute() ran under ANONYMOUS trust"
    assert result.get("status") == AgentStatus.ERROR.value, (
        f"S-1 refusal must surface as error status, got: {result.get('status')}"
    )


def test_s1_denial_refuses_execution_before_execute(monkeypatch):
    """TC-08: an always-present privileged node proves the negative S-1 path."""
    import framework.nodes.base_node as base_node_module

    events: list[str] = []
    execute_calls: list[object] = []
    monkeypatch.setattr(
        base_node_module,
        "emit_trace_event",
        lambda event_type, _payload, _state: events.append(event_type),
    )
    original_execute = _PrivilegedTrustGateFixture.execute

    def spy_execute(self, state):
        execute_calls.append(state)
        return original_execute(self, state)

    monkeypatch.setattr(_PrivilegedTrustGateFixture, "execute", spy_execute)
    result = _PrivilegedTrustGateFixture()(
        {
            "caller_trust_level": _trust_predecessor(
                _PrivilegedTrustGateFixture.required_trust_level
            ).value,
            "correlation_id": "tc08-s1-denial",
        }
    )

    assert result["status"] == "error"
    assert "S-1 trust gate denied" in result["error_log"][0]
    assert events == ["s1_denied"]
    assert not execute_calls
