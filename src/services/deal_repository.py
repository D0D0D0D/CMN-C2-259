"""Persistence-facing repository for the deal-scoring domain."""

from __future__ import annotations

import re
from typing import Any

from shared.services.database.base_db import BaseDatabase


_DEAL_COLUMNS = (
    "deal_id",
    "deal_name",
    "account_name",
    "owner",
    "phase",
    "amount",
    "last_updated",
    "bant_budget",
    "bant_authority",
    "bant_need",
    "bant_timeline",
    "notes",
)
_BANT_COLUMNS = ("bant_budget", "bant_authority", "bant_need", "bant_timeline")


class DealRepository:
    """Domain repository; callers do not depend on SQL or database behavior."""

    def __init__(self, database: BaseDatabase, table: str = "deals", *, owns_database: bool = False):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table or ""):
            raise ValueError(f"Invalid table identifier: {table!r}")
        self._database = database
        self._table = table
        self._owns_database = owns_database

    def list_deals(self, *, owner: str | None = None, phase: str | None = None) -> list[dict]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if owner:
            clauses.append("owner = :owner")
            params["owner"] = owner
        if phase:
            clauses.append("phase = :phase")
            params["phase"] = phase
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._database.fetch_many(f"SELECT * FROM {self._table}{where}", params)
        return [self._normalize_row(row) for row in rows]

    def get_deal(self, deal_id: str) -> dict | None:
        if not deal_id:
            return None
        row = self._database.fetch_one(
            f"SELECT * FROM {self._table} WHERE deal_id = :deal_id",
            {"deal_id": deal_id},
        )
        return self._normalize_row(row) if row is not None else None

    @staticmethod
    def _normalize_row(row: dict) -> dict:
        normalized = dict(row)
        for column in _BANT_COLUMNS:
            value = normalized.get(column)
            if isinstance(value, int) and value in (0, 1):
                normalized[column] = bool(value)
        return normalized

    def add_deal(self, deal: dict[str, Any]) -> None:
        """Register one source deal; duplicate IDs are rejected by the store."""

        row = {column: deal.get(column) for column in _DEAL_COLUMNS}
        if not row["deal_id"]:
            raise ValueError("deal_id is required")
        columns = ", ".join(_DEAL_COLUMNS)
        values = ", ".join(f":{column}" for column in _DEAL_COLUMNS)
        self._database.execute(
            f"INSERT INTO {self._table} ({columns}) VALUES ({values})",
            row,
        )

    def close(self) -> None:
        if self._owns_database:
            close = getattr(self._database, "close", None)
            if close is not None:
                close()
