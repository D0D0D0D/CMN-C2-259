"""Construction seam for the deal repository and its persistence backend."""

from __future__ import annotations

from shared.services.database.base_db import BaseDatabase

from src.services.deal_repository import DealRepository
from src.services.sqlite_database import SQLiteDatabase


def create_deal_repository(
    sqlite_path: str | None,
    *,
    database: BaseDatabase | None = None,
    table: str = "deals",
) -> DealRepository:
    """Create the domain repository, with an explicit backend injection seam.

    ``database`` supports tests and a future implemented backend. Backend
    selection remains entirely at this factory seam.
    """

    if database is not None:
        return DealRepository(database, table)

    if sqlite_path is None:
        raise ValueError("SQLite database path secret is required")
    return DealRepository(SQLiteDatabase(sqlite_path), table, owns_database=True)
