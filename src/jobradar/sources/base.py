"""Small shared contract for vacancy sources."""

from dataclasses import dataclass
from typing import Iterator, Protocol


class SourceError(RuntimeError):
    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


@dataclass(frozen=True)
class RawVacancy:
    source: str
    external_id: str
    url: str
    title: str
    employer: str | None
    description: str
    published_at: str | None = None


@dataclass(frozen=True)
class SourceEvent:
    kind: str
    code: str
    message: str
    external_id: str | None = None
    url: str | None = None
    stage: str = "source"


class SourceAdapter(Protocol):
    name: str

    def search(self, query: dict, *, limit: int, deadline: float) -> Iterator[RawVacancy | SourceEvent]: ...
    def fetch_details(self, external_id: str) -> RawVacancy: ...
    def check_status(self, external_id: str) -> str: ...
