"""Composition root: wire components and serialize turns for one user's chat."""

import threading
import sqlite3

from jobradar.dialogue.agent import DialogueAgent
from jobradar.llm.client import DeepSeekClient
from jobradar.profiles import ProfileError, ProfileManager
from jobradar.events import collect

_guard = threading.Lock()
_locks = {}


class Orchestrator:
    def __init__(self, database, settings, *, user_id=1, session_id=None, client=None, registry=None):
        manager = (ProfileManager.start_session(database, user_id) if session_id is None
                   else ProfileManager(database, user_id, session_id))
        from jobradar.tools.dispatcher import ToolDispatcher
        client = client or DeepSeekClient(settings)
        if registry is None:
            from jobradar.search.service import SearchService
            from jobradar.sources.hh import HHAdapter
            from jobradar.tools.registry import ToolRegistry
            registry = ToolRegistry()
            SearchService(database,settings,client,{"hh":HHAdapter(settings)}).register(registry)
        self.agent = DialogueAgent(manager, client, settings,
                                   ToolDispatcher(manager, registry))
        self.session_id = manager.session_id
        with _guard:
            self.lock = _locks.setdefault((str(database.path), user_id), threading.RLock())

    def chat(self, message):
        return collect(self.events(message))

    def events(self, message):
        with self.lock:
            try:
                return (yield from self.agent.events(message))
            except ProfileError as exc:
                self.agent.pending = None
                result = {"answer":"Не удалось обработать запрос: сессия или контекст недоступны.",
                          "calls":[], "error":{"code":exc.code, "retryable":False}}
            except sqlite3.Error:
                self.agent.pending = None
                result = {"answer":"Не удалось обработать запрос: проверьте доступность сессии и базы данных.",
                          "calls":[], "error":{"code":"DB_ERROR", "retryable":False}}
            yield {"event":"answer","data":result}
            return result
