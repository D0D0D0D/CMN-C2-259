"""Backend-independent acceptance contract for deal persistence.

Backend implementations are registered only in the fixture. Assertions use
the public repository API and make no assumptions about SQL, LIKE matching,
tokenization, or backend internals.
"""

from __future__ import annotations

import pytest

from shared.services.database.base_db import BaseDatabase

from src.services.deal_repository_factory import create_deal_repository


@pytest.fixture()
def repository(tmp_path):
    repository = create_deal_repository(str(tmp_path / "deals.sqlite3"))
    yield repository
    repository.close()


def _deal(deal_id: str, *, owner: str, phase: str) -> dict:
    return {
        "deal_id": deal_id,
        "deal_name": f"Deal {deal_id}",
        "account_name": f"Account {deal_id}",
        "owner": owner,
        "phase": phase,
        "amount": 1_000_000,
        "last_updated": "2026-08-31",
        "bant_budget": True,
        "bant_authority": False,
        "bant_need": True,
        "bant_timeline": None,
        "notes": "customer requested a proposal",
    }


def test_registered_deal_round_trips_through_public_port(repository):
    repository.add_deal(_deal("D-1", owner="alice", phase="proposal"))

    stored = repository.get_deal("D-1")

    assert stored is not None
    assert stored["deal_id"] == "D-1"
    assert stored["account_name"] == "Account D-1"
    assert stored["bant_budget"] is True
    assert stored["bant_authority"] is False
    assert repository.get_deal("missing") is None


def test_get_deal_matches_complete_id_only(repository):
    repository.add_deal(_deal("D-10", owner="alice", phase="proposal"))

    assert repository.get_deal("D-1") is None

    repository.add_deal(_deal("D-1", owner="alice", phase="proposal"))
    assert repository.get_deal("D-1")["deal_id"] == "D-1"


def test_filters_have_exact_domain_semantics(repository):
    repository.add_deal(_deal("D-1", owner="alice", phase="proposal"))
    repository.add_deal(_deal("D-2", owner="alice", phase="proposal_review"))
    repository.add_deal(_deal("D-3", owner="alice2", phase="proposal"))
    repository.add_deal(_deal("D-4", owner="ali", phase="won"))

    assert {row["deal_id"] for row in repository.list_deals(owner="alice")} == {"D-1", "D-2"}
    assert {row["deal_id"] for row in repository.list_deals(phase="proposal")} == {"D-1", "D-3"}
    assert [row["deal_id"] for row in repository.list_deals(owner="ali")] == ["D-4"]
    assert [row["deal_id"] for row in repository.list_deals(owner="alice", phase="proposal")] == ["D-1"]


def test_file_backed_store_survives_repository_reconstruction(tmp_path):
    path = str(tmp_path / "persistent-deals.sqlite3")
    first = create_deal_repository(path)
    first.add_deal(_deal("D-1", owner="alice", phase="proposal"))
    first.close()

    second = create_deal_repository(path)
    try:
        assert second.get_deal("D-1")["deal_name"] == "Deal D-1"
    finally:
        second.close()


class _RecordingDatabase(BaseDatabase):
    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    def fetch_one(self, query: str, params: dict | None = None):
        self.calls.append(("fetch_one", params))
        return None

    def fetch_many(self, query: str, params: dict | None = None):
        self.calls.append(("fetch_many", params))
        return [{"deal_id": "from-injected-backend"}]

    def execute(self, query: str, params: dict | None = None):
        self.calls.append(("execute", params))
        return 1


def test_factory_prefers_injected_backend_over_sqlite_path(tmp_path):
    injected = _RecordingDatabase()
    sqlite_path = tmp_path / "must-not-be-opened.sqlite3"

    repository = create_deal_repository(str(sqlite_path), database=injected)
    rows = repository.list_deals(owner="alice", phase="proposal")

    assert rows == [{"deal_id": "from-injected-backend"}]
    assert injected.calls == [("fetch_many", {"owner": "alice", "phase": "proposal"})]
    assert not sqlite_path.exists()
