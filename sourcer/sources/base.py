"""Source adapter contract.

A source yields Candidate objects. Everything downstream (dedup, scoring, PDF
storage, querying) is source-agnostic, so adding a new intake route means
writing one class - nothing else changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol, runtime_checkable

from ..models import JD, Candidate


@dataclass
class FetchContext:
    """What the runner tells a source about this run."""
    jds: list[JD] = field(default_factory=list)
    max_results: int = 200
    since_cursor: str | None = None
    dry_run: bool = False
    manual: bool = False                       # a person pressed "start" - ignore the clock window
    log: Callable[[str], None] = print         # live progress, shown in the UI
    should_stop: Callable[[], bool] = lambda: False


@dataclass
class FetchResult:
    candidates: list[Candidate] = field(default_factory=list)
    cursor: str | None = None
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@runtime_checkable
class Source(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """(usable, reason) - checked before the runner calls fetch()."""

    def fetch(self, ctx: FetchContext) -> FetchResult:
        ...


class BaseSource:
    name = "base"

    def available(self) -> tuple[bool, str]:
        return True, "ok"

    def fetch(self, ctx: FetchContext) -> FetchResult:  # pragma: no cover
        raise NotImplementedError

    @staticmethod
    def _iter_limited(it: Iterable, limit: int):
        for i, x in enumerate(it):
            if i >= limit:
                break
            yield x
