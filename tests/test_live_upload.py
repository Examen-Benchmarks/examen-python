"""CLI delivery during execution, with explicit HTTP and retry interfaces."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from examen import AsyncBench, Case, Ref
from examen.cli.discovery import DiscoveredBench
from examen.cli.main import _run
from examen.upload import Uploader


class Value(BaseModel):
    value: int


def definition(executed: list[int], *, fail: bool = False) -> DiscoveredBench:
    bench = AsyncBench(Ref(key="p", name="Project"), Ref(key="b", name="Bench"))

    @bench.experiment[Value, Value](
        key="experiment",
        name="Experiment",
        cases=[
            Case[Value, Value](
                key=str(index),
                name=str(index),
                input=Value(value=index),
                output=Value(value=index),
            )
            for index in range(3)
        ],
        scorers=[],
    )
    async def experiment(input: Value) -> Value:
        executed.append(input.value)
        if fail:
            raise RuntimeError("provider failed")
        return input

    return DiscoveredBench(source=Path(__file__), variable="bench", bench=bench)


async def no_delay(seconds: float) -> None:
    pass


def pending_records(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in directory.rglob("runs/*.json")]


async def acknowledge(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(201, json={"run": {"id": body["run"]["id"]}})


@pytest.mark.parametrize(("status", "attempts_per_run"), [(503, 2), (401, 1)])
async def test_live_upload_outage_finishes_execution_and_can_replay(
    tmp_path: Path, status: int, attempts_per_run: int
) -> None:
    executed: list[int] = []
    requests: list[str] = []
    directory = tmp_path / "results"

    async def unavailable(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        run_id = body["run"]["id"]
        # The request is sent only after its complete payload is on disk.
        path = next(directory.rglob(f"runs/{run_id}.json"))
        assert json.loads(path.read_text())["payload"] == body
        # Serial execution proves delivery begins before the next case runs.
        assert len(executed) == len(set(requests) | {run_id})
        requests.append(run_id)
        return httpx.Response(status, text="unavailable")

    client = Uploader(
        "https://examen.test",
        "secret",
        transport=httpx.MockTransport(unavailable),
        sleep=no_delay,
        max_attempts=2,
    )
    exit_code = await _run(
        [definition(executed)],
        directory,
        version={},
        concurrency=1,
        filter_pattern=None,
        uploader=client,
    )
    assert exit_code == 1
    assert executed == [0, 1, 2]
    saved = pending_records(directory)
    assert len(saved) == 3
    assert all(not record["uploaded"] and record["last_error"] for record in saved)
    assert len(set(requests)) == 3
    assert all(requests.count(run_id) == attempts_per_run for run_id in set(requests))
    manifest = json.loads(next(directory.rglob("manifest.json")).read_text())
    assert manifest["status"] == "completed"
    assert manifest["completed"] == manifest["succeeded"] == 3

    recovered_ids: list[str] = []

    async def recovered(request: httpx.Request) -> httpx.Response:
        recovered_ids.append(json.loads(request.content)["run"]["id"])
        return await acknowledge(request)

    replay = Uploader("https://examen.test", "secret", transport=httpx.MockTransport(recovered))
    result = await replay.upload(directory)
    assert (result.uploaded, result.pending) == (3, 0)
    assert set(recovered_ids) == set(requests)
    assert len(recovered_ids) == 3
    assert all(record["uploaded"] for record in pending_records(directory))
    assert (await replay.upload(directory)).uploaded == 0
    assert len(recovered_ids) == 3
    assert executed == [0, 1, 2]


@pytest.mark.parametrize("fail", [False, True])
async def test_interrupt_during_live_upload_counts_saved_results_and_allows_replay(
    tmp_path: Path, fail: bool
) -> None:
    executed: list[int] = []
    uploading = asyncio.Event()
    release = asyncio.Event()
    directory = tmp_path / "results"

    async def blocked(request: httpx.Request) -> httpx.Response:
        uploading.set()
        await release.wait()
        return await acknowledge(request)

    client = Uploader("https://examen.test", "secret", transport=httpx.MockTransport(blocked))
    task = asyncio.create_task(
        _run(
            [definition(executed, fail=fail)],
            directory,
            version={},
            concurrency=1,
            filter_pattern=None,
            uploader=client,
        )
    )
    try:
        await asyncio.wait_for(uploading.wait(), timeout=5)
        assert len(pending_records(directory)) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert executed == [0]
    saved = pending_records(directory)
    assert len(saved) == 1 and not saved[0]["uploaded"]
    assert saved[0]["destination"] == "https://examen.test"
    manifest = json.loads(next(directory.rglob("manifest.json")).read_text())
    assert manifest["status"] == "interrupted"
    assert manifest["finished_at"] is not None
    assert manifest["completed"] == 1
    assert manifest["succeeded"] == int(not fail)
    assert manifest["errored"] == int(fail)

    replay = Uploader("https://examen.test", "secret", transport=httpx.MockTransport(acknowledge))
    result = await replay.upload(directory)
    assert (result.uploaded, result.pending) == (1, 0)
    assert pending_records(directory)[0]["uploaded"]
    assert executed == [0]
