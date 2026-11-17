"""Minimal structured diagnostics; never log prompts, source texts or credentials."""

import json
import logging
from datetime import datetime, timezone


def record(component, event, **fields):
    logging.getLogger("jobradar").info(json.dumps({
        "time":datetime.now(timezone.utc).isoformat(),"level":"INFO",
        "component":component,"event":event,**fields},ensure_ascii=False))
