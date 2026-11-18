"""Approved definitions and an explicit handler registry; no dynamic plugins."""

import copy
import json
from importlib.resources import files

DEFINITIONS = {item["function"]["name"]: item for item in
               json.loads(files("jobradar.tools").joinpath("definitions.json").read_text(encoding="utf-8"))}
GENERAL = frozenset(("list_profiles", "create_profile", "switch_context"))
ACTIVE = frozenset(DEFINITIONS) - {"create_profile"}
READ_ONLY = frozenset(("list_profiles", "list_contexts", "get_search_results", "get_vacancy_details"))


def allowed_names(state):
    return GENERAL if state["active_context_id"] is None else ACTIVE


def definitions(state):
    return [copy.deepcopy(value) for key, value in DEFINITIONS.items() if key in allowed_names(state)]


class ToolRegistry:
    def __init__(self):
        self.handlers = {}
        self.diagnostics_reader = None

    def register(self, name, handler):
        if name not in DEFINITIONS or name in self.handlers:
            raise ValueError("Unknown or duplicate tool")
        self.handlers[name] = handler
