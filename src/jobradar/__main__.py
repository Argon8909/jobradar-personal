"""Initialize SQLite or run the v0.1 dialogue in a local terminal."""

import argparse
import sqlite3
from pathlib import Path

from .config import load_settings
from .storage import Database, SchemaError


def main() -> int:
    parser = argparse.ArgumentParser(description="JobRadar v0.1")
    parser.add_argument("command", choices=["init-db", "chat"])
    parser.add_argument("--config", type=Path, default=Path("config/v0.1.json"))
    parser.add_argument("--session", type=int, help="Resume a saved web/CLI session")
    args = parser.parse_args()
    try:
        settings = load_settings(args.config)
        database = Database(settings.database_path, busy_timeout_ms=settings.busy_timeout_ms)
        database.initialize()
    except (OSError, ValueError, sqlite3.Error, SchemaError) as error:
        parser.exit(1, f"Initialization failed: {error}\n")
    if args.command == "init-db":
        print(f"SQLite v0.1 initialized: {settings.database_path}")
        return 0
    from .orchestration.service import Orchestrator
    orchestrator = Orchestrator(database, settings, session_id=args.session)
    print(f"JobRadar v0.1 — сессия {orchestrator.session_id}. Для выхода: /exit")
    while True:
        try:
            message = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if message == "/exit":
            break
        if message:
            print(orchestrator.chat(message)["answer"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
