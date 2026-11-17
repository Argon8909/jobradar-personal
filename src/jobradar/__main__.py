"""Stage 1 CLI: initialize a fresh local database without calling external services."""

import argparse
import sqlite3
from pathlib import Path

from .config import load_settings
from .storage import Database, SchemaError


def main() -> int:
    parser = argparse.ArgumentParser(description="JobRadar v0.1")
    parser.add_argument("command", choices=["init-db"])
    parser.add_argument("--config", type=Path, default=Path("config/v0.1.json"))
    args = parser.parse_args()
    try:
        settings = load_settings(args.config)
        Database(settings.database_path, busy_timeout_ms=settings.busy_timeout_ms).initialize()
    except (OSError, ValueError, sqlite3.Error, SchemaError) as error:
        parser.exit(1, f"Initialization failed: {error}\n")
    print(f"SQLite v0.1 initialized: {settings.database_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
