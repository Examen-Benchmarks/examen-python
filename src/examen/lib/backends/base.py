"""Backend protocol — the contract every backend implementation must satisfy."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Backend(Protocol):
    """A sink for ingested runs.

    Implementations: `Connector` (HTTP to examen-backend). Local SQLite is
    planned. All backends accept the same self-contained payload (project,
    bench, experiment, case, version, run, metrics) and find-or-create parents
    by name.
    """

    async def ingest_run(self, payload: dict[str, Any]) -> dict[str, Any]: ...
