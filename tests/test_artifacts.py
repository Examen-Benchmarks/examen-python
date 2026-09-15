"""Results survive report failures, and invalid storage fails before execution."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from examen import AsyncBench, Backend, Case, Ref, RunProgress


class Input(BaseModel):
    value: int = 1


class Output(BaseModel):
    value: int = 1


class FailedReport:
    async def ingest_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise ConnectionError("report unavailable")

    async def close(self) -> None:
        pass


def make_bench(executed: list[int], backends: list[Backend] | None = None) -> AsyncBench:
    bench = AsyncBench(Ref(key="p", name="P"), Ref(key="b", name="B"), backends=backends or [])

    @bench.experiment[Input, Output](
        key="e",
        name="E",
        cases=[Case[Input, Output](key="c", name="C", input=Input(), output=Output(), repeats=3)],
        scorers=[],
    )
    async def experiment(input: Input) -> Output:
        executed.append(input.value)
        return Output(value=input.value)

    return bench


async def test_report_failure_does_not_lose_or_cancel_completed_work(tmp_path: Path) -> None:
    executed: list[int] = []
    bench = make_bench(executed, [FailedReport()])
    with pytest.raises(RuntimeError, match="results are saved"):
        await bench.run({}, output_dir=tmp_path, concurrency=1)
    assert executed == [1, 1, 1]
    assert len(list(tmp_path.rglob("runs/*.json"))) == 3
    manifest = json.loads(next(tmp_path.rglob("manifest.json")).read_text())
    assert manifest["status"] == "error" and manifest["completed"] == 3


async def test_progress_only_reports_results_already_on_disk(tmp_path: Path) -> None:
    counts: list[int] = []

    def progress(event: RunProgress) -> None:
        saved = list(tmp_path.rglob("runs/*.json"))
        assert len(saved) == event.completed
        counts.append(event.completed)

    result = await make_bench([]).run({}, output_dir=tmp_path, on_progress=progress)
    assert counts == [1, 2, 3]
    assert result.path.is_dir() and result.succeeded == 3


async def test_invalid_storage_and_version_fail_before_execution(tmp_path: Path) -> None:
    executed: list[int] = []
    bench = make_bench(executed)
    invalid = tmp_path / "file"
    invalid.write_text("existing")
    with pytest.raises(OSError):
        await bench.run({}, output_dir=invalid)
    with pytest.raises(ValueError):
        await bench.run({"value": float("nan")}, output_dir=tmp_path)
    assert executed == []
    assert not list(tmp_path.rglob("manifest.json"))
