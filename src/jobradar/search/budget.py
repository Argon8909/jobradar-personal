"""Task-local technical limits; no financial budget or external scheduler."""

import time


class SearchStop(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class TaskBudget:
    def __init__(self, settings, *, clock=time.monotonic):
        self.clock = clock
        self.deadline = clock() + settings.values["search"]["deadline_seconds"]
        self.max_calls = settings.values["search"].get("max_llm_calls",100)
        self.calls = 0

    def check(self):
        if self.clock() >= self.deadline:
            raise SearchStop("TIMEOUT", "Достигнут предел времени поиска.")

    def consume(self):
        self.check()
        if self.calls >= self.max_calls:
            raise SearchStop("LLM_LIMIT", "Достигнут предел обращений к модели для поиска.")
        self.calls += 1
