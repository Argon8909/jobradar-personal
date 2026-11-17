"""One SQLite connection per operation; never opens the legacy database by default."""

import sqlite3
from contextlib import closing, contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1
APPLICATION_ID = 0x4A523031  # JR01; SQLite metadata, not a domain table.


class SchemaError(RuntimeError):
    """The file is not a database owned by this schema version."""


def normalize_tag(value: str) -> str:
    return " ".join(value.split()).casefold()


class Database:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 15000):
        self.path = Path(path).expanduser().resolve()
        if type(busy_timeout_ms) is not int or busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be a positive integer")
        self.busy_timeout_ms = busy_timeout_ms

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Open an existing file without creating it as a side effect of a read."""
        db = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True,
            timeout=self.busy_timeout_ms / 1000, isolation_level=None,
        )
        try:
            db.row_factory = sqlite3.Row
            db.create_function("normalize_tag", 1, normalize_tag, deterministic=True)
            db.execute("PRAGMA foreign_keys = ON")
            db.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def initialize(self) -> None:
        """Create a clean v0.1 schema atomically; refuse legacy or unknown schemas."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # sqlite3 creates the file only during this explicit initialization operation.
        with closing(sqlite3.connect(self.path)) as initial:
            initial.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            application_id = db.execute("PRAGMA application_id").fetchone()[0]
            existing = db.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if version == SCHEMA_VERSION and application_id == APPLICATION_ID:
                return
            if existing or version or application_id:
                raise SchemaError("Existing schema is not JobRadar v0.1; choose a new database path")

            script = files("jobradar.storage").joinpath("schema.sql").read_text(encoding="utf-8")
            statement = ""
            # executescript would implicitly commit and break atomic initialization.
            for line in script.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            if statement.strip():
                raise SchemaError("Incomplete schema SQL")
            db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            db.execute("INSERT INTO users(id, name) VALUES (1, 'Локальный пользователь')")
