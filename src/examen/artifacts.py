"""Durable JSON copies of runs, retained even after successful delivery."""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class StoredRun(BaseModel):
    """Local delivery record. Credentials are never written to the file."""

    format_version: int = 1
    destination: str | None = None
    payload: dict[str, Any]
    uploaded: bool = False
    last_error: str | None = None


class ExperimentInfo(BaseModel):
    """A discoverable experiment and its execution size."""

    path: str
    name: str
    cases: int
    runs: int


class RunProgress(BaseModel):
    """Progress emitted after a completed result has reached disk."""

    completed: int
    total: int
    succeeded: int
    errored: int
    experiment: str
    case: str


class RunResult(BaseModel):
    """Manifest for one invocation of a bench, stored beside its run records."""

    format_version: Literal[1] = 1
    execution_id: str
    path: Path
    project_key: str
    bench_key: str
    version: dict[str, Any]
    status: Literal["running", "completed", "interrupted", "error"] = "running"
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    total: int
    completed: int = 0
    succeeded: int = 0
    errored: int = 0


class SyncResult(BaseModel):
    """Outcome of a sync attempt; pending runs remain recoverable on disk."""

    uploaded: int = 0
    pending: int = 0
    errors: dict[str, str] = Field(default_factory=dict)


def write_record(path: Path, record: BaseModel) -> None:
    """Replace atomically, flushing both data and the directory entry to disk."""
    data = record.model_dump_json(indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".run-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_run(directory: Path, payload: dict[str, Any]) -> Path:
    # Snapshot before handing control back to the caller. Each invocation gets
    # its own UUID, including repeats with otherwise identical payloads.
    snapshot = json.loads(json.dumps(payload, allow_nan=False))
    run_id = str(UUID(snapshot["run"]["id"])) if "id" in snapshot["run"] else str(uuid4())
    snapshot["run"]["id"] = run_id
    path = directory / f"{run_id}.json"
    if path.exists():
        raise FileExistsError(f"Run already saved: {path}")
    write_record(path, StoredRun(payload=snapshot))
    return path


def read_record(path: Path, destination: str | None = None) -> StoredRun:
    record = StoredRun.model_validate_json(path.read_text(encoding="utf-8"))
    if record.format_version != 1:
        raise ValueError(f"Unsupported record format: {record.format_version}")
    if destination is not None and record.destination not in (None, destination):
        raise ValueError("Stored run belongs to a different backend URL")
    if str(UUID(str(record.payload["run"]["id"]))) != path.stem:
        raise ValueError("Stored run ID does not match its filename")
    return record
