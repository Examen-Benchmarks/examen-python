"""Backend protocol — the contract every backend implementation must satisfy."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Backend(Protocol):
    """A sink for ingested runs.

    Optional sinks such as `LocalReportBackend` receive the same self-contained
    keyed payload after the runner has saved it to JSON. HTTP uploading is a
    separate operation in `examen.upload`, not a benchmark backend.

    The bench calls ``close()`` on every backend at the end of ``run()``, fanned
    out in parallel. Use it to flush buffered state, render artifacts, or close
    resources. ``close()`` must be idempotent — repeated calls (e.g. running the
    same bench twice) must not raise.
    """

    async def ingest_run(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def close(self) -> None: ...
