"""A queue that records instead of enqueuing.

The API's job is to accept a message and hand it on; whether the worker then
does the right thing is a separate question with its own tests. Recording the
call keeps those two apart - and makes "was a send actually queued?" something a
test can assert rather than infer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EnqueuedJob:
    name: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


@dataclass
class FakeQueue:
    """Stands in for the ARQ pool."""

    jobs: list[EnqueuedJob] = field(default_factory=list)

    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.jobs.append(EnqueuedJob(name=name, args=args, kwargs=kwargs))

    async def aclose(self) -> None:
        return None

    # -- reading it back ----------------------------------------------------

    @property
    def names(self) -> list[str]:
        return [job.name for job in self.jobs]

    def ids_for(self, name: str) -> list[Any]:
        """The first argument of every job of this name - the email id, here."""
        return [job.args[0] for job in self.jobs if job.name == name and job.args]
