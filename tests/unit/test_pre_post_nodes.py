# CMN-C2-259 — Unit Tests: pre_process / post_process nodes

from __future__ import annotations

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode
from src.services import deal_repository_factory

CFG = {
    "database": {"engine": "sqlite", "table": "deals"},
    "column_map": {},
    "low_coverage_flag_ratio": 0.4,
}


class FakeRepository:
    def __init__(self, rows):
        self.rows = rows

    def list_deals(self, *, owner=None, phase=None):
        return list(self.rows)

    def close(self):
        pass


def _pre_node():
    """PreProcessNode with the trusted agent_config injected (as register_nodes does)."""
    node = PreProcessNode()
    node.agent_config = CFG
    return node


def _pre_state():
    # A caller-supplied _config here MUST be ignored in favor of agent_config.
    return {"input_context": {"_config": {"database": {"table": "EVIL"}}}, "user_input": "run"}


def test_pre_process_fetch_and_validate(monkeypatch):
    rows = [{"deal_id": "D1", "deal_name": "A", "account_name": "AC",
             "phase": "won", "last_updated": "2026-07-01", "notes": ""}]
    monkeypatch.setattr(
        deal_repository_factory,
        "create_deal_repository",
        lambda path, table: FakeRepository(rows),
    )
    out = _pre_node().execute(_pre_state())
    assert out["status"] == AgentStatus.SUCCESS
    assert len(out["deals"]) == 1
    assert out["non_conformant"] is False
    # caller-supplied _config was ignored; trusted table is used
    assert out["input_context"]["_config"]["database"]["table"] == "deals"


def test_pre_process_skips_rows_without_deal_id(monkeypatch):
    rows = [{"deal_name": "no id"}, {"deal_id": "D2", "phase": "won"}]
    monkeypatch.setattr(
        deal_repository_factory,
        "create_deal_repository",
        lambda path, table: FakeRepository(rows),
    )
    out = _pre_node().execute(_pre_state())
    assert len(out["deals"]) == 1  # row without deal_id skipped
    assert out["non_conformant"] is False  # not ALL rows missing id


def test_pre_process_non_conformant_all_missing_id(monkeypatch):
    rows = [{"deal_name": "x"}, {"phase": "won"}]  # none has deal_id
    monkeypatch.setattr(
        deal_repository_factory,
        "create_deal_repository",
        lambda path, table: FakeRepository(rows),
    )
    out = _pre_node().execute(_pre_state())
    assert out["deals"] == []
    assert out["non_conformant"] is True
    assert out["status"] == AgentStatus.SUCCESS  # not an error


def test_pre_process_db_failure_is_error(monkeypatch):
    def boom(path, table):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(deal_repository_factory, "create_deal_repository", boom)
    out = _pre_node().execute(_pre_state())
    assert out["status"] == AgentStatus.ERROR
    assert out.get("error_log")


def test_post_process_builds_report():
    state = {
        "scored_deals": [{"deal_id": "D1", "deal_name": "A", "account_name": "AC",
                          "score": 0.8, "coverage": 1.0, "priority": "P2-high", "_stagnant": False}],
        "qualitative": {"D1": {"risks": ["r"], "actions": ["a"]}},
        "missing_map": {}, "non_conformant": False,
        "input_context": {"_config": CFG},
    }
    out = PostProcessNode().execute(state)
    assert out["status"] == AgentStatus.SUCCESS
    assert "商談スコアリング" in out["output"]


def test_post_process_passes_through_upstream_error():
    out = PostProcessNode().execute({"status": AgentStatus.ERROR.value})
    assert out == {}  # no override of an upstream error


def test_s2_masks_pii_in_all_string_fields():
    # notes containing an email must be masked in place (not just flagged).
    node = _pre_node()
    state = {"deals": [{"deal_id": "D1", "notes": "連絡先は taro@example.com です"}]}
    out = node._extra_security_gate_input(state)
    masked_notes = out["deals"][0]["notes"]
    assert "taro@example.com" not in masked_notes
    assert out["deals"][0].get("_pii_flag") is True


def test_s1_trust_gate_denies_anonymous_caller():
    # S-1: a privileged node requires VERIFIED_EXTERNAL; an ANONYMOUS
    # caller is refused by BaseNode.__call__ before execute() runs.
    node = _pre_node()
    out = node({"caller_trust_level": TrustLevel.ANONYMOUS.value, "input_context": {}})
    assert out["status"] == AgentStatus.ERROR
    assert any("S-1 trust gate denied" in e for e in out.get("error_log", []))


def test_s1_declared_trust_level_is_valid_enum():
    assert PreProcessNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL
