"""HTTP delivery of plain JSON artifacts. No benchmark discovery or execution."""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from examen.artifacts import SyncResult, read_record, write_record

logger = logging.getLogger(__name__)


class Uploader:
    """Deliver saved JSON runs independently of benchmark execution.

    Retries transport errors, 408, 429 and 5xx responses. Completed records are
    retained; subsequent uploads skip them. Stable run IDs make retries safe
    even when the server commits but its response is lost.

    ``transport`` and ``sleep`` are explicit interfaces for HTTP and backoff.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        max_attempts: int = 10,
        retry_delay: float = 1.0,
        max_retry_delay: float = 30.0,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if retry_delay < 0 or max_retry_delay < 0 or timeout <= 0:
            raise ValueError("Retry delays must be nonnegative and timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self.max_retry_delay = max_retry_delay
        self.timeout = timeout
        self._transport = transport
        self._sleep = sleep
        self._sync_lock = asyncio.Lock()
        self.last_sync: SyncResult | None = None

    async def upload(self, path: str | Path) -> SyncResult:
        """Upload a saved execution directory, runs directory, or single JSON run.

        Files are bound to the backend URL before their first delivery attempt.
        Unacknowledged records survive errors and cancellation for later replay.
        """
        source = Path(path).resolve()
        if source.is_file():
            paths = (
                sorted((source.parent / "runs").glob("*.json"))
                if source.name == "manifest.json"
                else [source]
            )
        elif source.is_dir():
            paths = sorted(
                set(source.rglob("runs/*.json"))
                | {p for p in source.glob("*.json") if p.name != "manifest.json"}
            )
        else:
            raise ValueError(f"Results path does not exist: {source}")
        if not paths:
            raise ValueError(f"No saved run JSON files found in {source}")
        async with self._sync_lock:
            result = SyncResult()
            pending: list[Path] = []
            for path in paths:
                try:
                    if not read_record(path, self.base_url).uploaded:
                        pending.append(path)
                except (ValueError, KeyError, TypeError) as exc:
                    result.errors[str(path)] = str(exc)
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self.timeout,
            ) as client:
                for attempt in range(self.max_attempts):
                    retry: list[Path] = []
                    for path in pending:
                        record = read_record(path, self.base_url)
                        # Another sync may have acknowledged it in the meantime.
                        if record.uploaded:
                            continue
                        if record.destination is None:
                            record.destination = self.base_url
                            write_record(path, record)
                        transient = False
                        try:
                            response = await client.post(
                                f"{self.base_url}/ingest/runs",
                                json=record.payload,
                                headers={"Authorization": f"Bearer {self.api_key}"},
                            )
                            response.raise_for_status()
                            body = response.json()
                            if body.get("run", {}).get("id") != record.payload["run"]["id"]:
                                raise ValueError(
                                    "Backend did not acknowledge the stored run ID; "
                                    "upgrade the backend to support idempotent ingestion"
                                )
                        except httpx.TransportError as exc:
                            record.last_error = f"{type(exc).__name__}: {exc}"
                            transient = True
                        except httpx.HTTPStatusError as exc:
                            status = exc.response.status_code
                            record.last_error = f"HTTP {status}: {exc.response.text}"
                            transient = status in (408, 429) or status >= 500
                        except (ValueError, AttributeError) as exc:
                            record.last_error = str(exc)
                        else:
                            record.uploaded = True
                            record.last_error = None
                            result.uploaded += 1
                            result.errors.pop(str(path), None)
                        write_record(path, record)
                        if not record.uploaded:
                            result.errors[str(path)] = record.last_error or "Upload failed"
                            if transient:
                                retry.append(path)
                    pending = retry
                    if not pending or attempt + 1 == self.max_attempts:
                        break
                    delay = min(self.max_retry_delay, self.retry_delay * 2**attempt)
                    await self._sleep(delay * random.uniform(0.5, 1.0))
            result.pending = len(result.errors)
            self.last_sync = result
            if result.pending:
                logger.warning(
                    "%d Examen uploads remain pending in %s. Results are saved; "
                    "run examen upload again to retry. Errors: %s",
                    result.pending,
                    source,
                    result.errors,
                )
            return result
