"""Backend protocol — the contract every backend implementation must satisfy."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Backend(Protocol):
    """A sink for ingested runs.

    Implementations: `Connector` (HTTP to examen-backend), `LocalReportBackend`
    (offline self-contained HTML). Local SQLite is planned. All backends accept
    the same self-contained payload (project, bench, experiment, case, version,
    run, metrics) and find-or-create parents by name.

    The bench calls ``close()`` on every backend at the end of ``run()``, fanned
    out in parallel. Use it to flush buffered state, render artifacts, or close
    resources. ``close()`` must be idempotent — repeated calls (e.g. running the
    same bench twice) must not raise.
    """

    async def ingest_run(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def close(self) -> None: ...
