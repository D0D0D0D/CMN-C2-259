"""SQLite-specific checks kept separate from the repository contract."""

from __future__ import annotations

import pytest

from src.services.sqlite_database import SQLiteDatabase


_INSERT = """
INSERT INTO deals (deal_id, account_name, phase, last_updated)
VALUES (:deal_id, :account_name, :phase, :last_updated)
"""


def _params(deal_id: str) -> dict:
    return {
        "deal_id": deal_id,
        "account_name": "Account",
        "phase": "proposal",
        "last_updated": "2026-08-31",
    }


def test_memory_database_is_shared_across_operation_connections():
    database = SQLiteDatabase(":memory:")
    try:
        database.execute(_INSERT, _params("D-1"))

        assert database.fetch_one(
            "SELECT deal_id FROM deals WHERE deal_id = :deal_id", {"deal_id": "D-1"}
        ) == {"deal_id": "D-1"}
    finally:
        database.close()


def test_transaction_rolls_back_all_writes_on_error():
    database = SQLiteDatabase(":memory:")
    try:
        with pytest.raises(RuntimeError, match="abort"):
            with database.transaction():
                database.execute(_INSERT, _params("D-1"))
                raise RuntimeError("abort")

        assert database.fetch_one(
            "SELECT deal_id FROM deals WHERE deal_id = :deal_id", {"deal_id": "D-1"}
        ) is None
    finally:
        database.close()
