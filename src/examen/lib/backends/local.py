"""Offline backend: render runs to a self-contained HTML file.

For users who don't want to deploy a server. Accumulates payloads in memory
during a bench run, then renders a single portable HTML file at ``close()``.

No re-scoring across processes, no comparison across runs — those need a
persistent store. The intended use is "I ran a bench locally, here's a file
I can open, archive, or attach to a PR".
"""

from pathlib import Path
from typing import Any

from examen.lib.report import render_html


class LocalReportBackend:
    """Backend that writes a self-contained HTML report on close.

    Args:
        path: Where to write the HTML file. Parent directories are created if
            needed. Existing files are overwritten.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._runs: list[dict[str, Any]] = []

    async def ingest_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._runs.append(payload)
        return {"ok": True}

    async def close(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(render_html(self._runs), encoding="utf-8")
