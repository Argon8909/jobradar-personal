"""Recheck mode, arguments, session and exact authorization for every operation."""

import json
import sqlite3
from types import GeneratorType

from jobradar.profiles import ProfileError, ProfileManager
from jobradar.profiles.contracts import validate, validate_profile_fields
from .registry import READ_ONLY, ToolRegistry, allowed_names
from jobradar.events import collect


def canonical(name, arguments):
    return json.dumps([name, arguments], ensure_ascii=False, sort_keys=True, allow_nan=False)


def failure(code, message, retryable=False):
    return {"status":"error", "error":{"code":code, "message":message, "retryable":retryable}}


class ToolDispatcher:
    def __init__(self, manager, registry=None):
        self.manager = manager
        self.registry = registry or ToolRegistry()
        for name in ("list_profiles", "create_profile", "switch_context", "return_to_general",
                     "update_profile", "list_contexts", "create_context", "update_context"):
            if name not in self.registry.handlers:
                self.registry.register(name, self._profile_handler(name))

    def search_diagnostics(self, state, task_id=None):
        if not self.registry.diagnostics_reader or state["active_context_id"] is None:
            return None
        guarded = ProfileManager(self.manager.database,self.manager.user_id,self.manager.session_id,
                                 expected_context_id=state["active_context_id"])
        try:
            return self.registry.diagnostics_reader(guarded,state,task_id)
        except ProfileError as exc:
            if exc.code == "TASK_NOT_FOUND":
                return None
            raise

    @staticmethod
    def _profile_handler(name):
        def run(manager, arguments, state):
            kwargs = {} if name in READ_ONLY else {"authorized":True}
            if name == "update_profile":
                kwargs["expected_revision"] = state["profile"]["revision"]
            if name == "update_context":
                kwargs["expected_revision"] = state["context"]["criteria_revision"]
            return getattr(manager, name)(**arguments, **kwargs)
        return run

    def execute(self, name, arguments, *, expected_state, grant=None, request_text=None):
        return collect(self.events(name,arguments,expected_state=expected_state,grant=grant,request_text=request_text))

    def events(self, name, arguments, *, expected_state, grant=None, request_text=None):
        """grant/expected_state are internal application data, never tool arguments."""
        try:
            if self.manager.get_state() != expected_state:
                return failure("REVISION_CONFLICT", "Контекст или данные изменились. Повторите запрос.")
            if name not in allowed_names(expected_state):
                return failure("TOOL_NOT_ALLOWED", "Команда недоступна в текущем режиме.")
            arguments = validate(name, arguments)
            if name in ("create_profile","update_profile"):
                validate_profile_fields(arguments.get("profile_data",{}) if name == "create_profile" else arguments["changes"])
            if name not in READ_ONLY and grant != canonical(name, arguments):
                return failure("CONFIRMATION_REQUIRED", "Для этого действия требуется согласие пользователя.")
            handler = self.registry.handlers.get(name)
            if handler is None:
                return failure("SOURCE_UNAVAILABLE", "Поиск и выдача вакансий ещё не подключены.")
            guarded = ProfileManager(self.manager.database, self.manager.user_id, self.manager.session_id,
                                     expected_context_id=expected_state["active_context_id"])
            if name == "search_vacancies" and "request" not in arguments and request_text:
                arguments = {**arguments,"request":request_text}
            data = handler(guarded, arguments, expected_state)
            if isinstance(data,GeneratorType):
                data = yield from data
            return {"status":"success", "data":data}
        except ProfileError as exc:
            return failure(exc.code, str(exc), exc.retryable)
        except sqlite3.Error:
            # No automatic replay of mutating operations: commit outcome may be unknown.
            return failure("DB_ERROR", "Не удалось выполнить операцию с базой данных.")
