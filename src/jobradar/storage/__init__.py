"""SQLite schema initialization and transaction boundary."""

from .database import Database, SchemaError

__all__ = ["Database", "SchemaError"]
