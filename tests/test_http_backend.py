"""Durability and delivery tests through explicit HTTP and clock interfaces."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from examen import AsyncBench, Case, ExactMatchScorer, Ref
from examen.artifacts import save_run
from examen.upload import Uploader


def payload() -> dict[str, Any]:
    return {"run": {"status": "succeeded", "trace": {"output": "paid result"}}, "metrics": []}


def records(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


class Server:
    def __init__(self, failures: int = 0, status: int = 503, lose_response: bool = False) -> None:
        self.failures = failures
        self.status = status
        self.lose_response = lose_response
        self.requests: list[dict[str, Any]] = []
        self.saved: dict[str, dict[str, Any]] = {}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if len(self.requests) <= self.failures:
            return httpx.Response(self.status, text="temporarily unavailable")
        run_id = body["run"]["id"]
        self.saved.setdefault(run_id, body)
        if self.lose_response and len(self.requests) == 1:
            raise httpx.ReadTimeout("Response lost after commit", request=request)
        return httpx.Response(201, json={"run": {"id": run_id}})


class Clock:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)


def uploader(server: Server, clock: Clock | None = None, **kwargs: Any) -> Uploader:
    return Uploader(
        "https://examen.test",
        "secret",
        transport=httpx.MockTransport(server),
        sleep=(clock or Clock()).sleep,
        **kwargs,
    )


def test_capture_is_durable_and_snapshots_payload(tmp_path: Path) -> None:
    data = payload()
    path = save_run(tmp_path, data)
    data["run"]["trace"]["output"] = "mutated"
    saved = records(tmp_path)[0]
    assert saved["payload"]["run"]["trace"]["output"] == "paid result"
    assert saved["payload"]["run"]["id"] == path.stem
    assert saved["destination"] is None
    assert not saved["uploaded"]


async def test_retries_then_retains_acknowledged_json_and_skips_on_resume(tmp_path: Path) -> None:
    server, clock = Server(failures=3), Clock()
    save_run(tmp_path, payload())
    result = await uploader(server, clock).upload(tmp_path)
    assert (result.uploaded, result.pending) == (1, 0)
    assert len(server.requests) == 4
    assert len(clock.delays) == 3
    assert all(0 < delay <= bound for delay, bound in zip(clock.delays, [1, 2, 4], strict=True))
    assert records(tmp_path)[0]["uploaded"]
    assert "secret" not in next(tmp_path.glob("*.json")).read_text()
    await uploader(server).upload(tmp_path)
    assert len(server.requests) == 4


async def test_exhaustion_retains_all_results_and_new_uploader_can_resume(tmp_path: Path) -> None:
    unavailable = Server(failures=100)
    client = uploader(unavailable, max_attempts=4)
    for _ in range(3):
        save_run(tmp_path, payload())
    result = await client.upload(tmp_path)
    assert result.pending == 3
    assert len(unavailable.requests) == 12
    assert len(records(tmp_path)) == 3
    assert all(not record["uploaded"] for record in records(tmp_path))
    recovered = Server()
    result = await uploader(recovered).upload(tmp_path)
    assert (result.uploaded, result.pending) == (3, 0)
    assert len(recovered.saved) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_permanent_http_errors_are_saved_without_retries(tmp_path: Path, status: int) -> None:
    server, clock = Server(failures=100, status=status), Clock()
    save_run(tmp_path, payload())
    result = await uploader(server, clock).upload(tmp_path)
    assert result.pending == 1
    assert len(server.requests) == 1
    assert not clock.delays
    assert f"HTTP {status}" in records(tmp_path)[0]["last_error"]


async def test_lost_response_retries_the_same_id(tmp_path: Path) -> None:
    server = Server(lose_response=True)
    save_run(tmp_path, payload())
    result = await uploader(server).upload(tmp_path)
    assert (result.uploaded, result.pending) == (1, 0)
    assert len(server.requests) == 2
    assert len(server.saved) == 1
    assert server.requests[0] == server.requests[1]


async def test_corrupt_record_does_not_block_other_saved_runs(tmp_path: Path) -> None:
    save_run(tmp_path, payload())
    (tmp_path / "broken.json").write_text("{")
    result = await uploader(Server()).upload(tmp_path)
    assert (result.uploaded, result.pending) == (1, 1)
    assert (tmp_path / "broken.json").read_text() == "{"


async def test_different_destination_is_never_uploaded(tmp_path: Path) -> None:
    save_run(tmp_path, payload())
    server = Server()
    await uploader(server).upload(tmp_path)
    other_server = Server()
    other = Uploader("https://other.test", "secret", transport=httpx.MockTransport(other_server))
    assert (await other.upload(tmp_path)).pending == 1
    assert not other_server.requests


async def test_cancellation_leaves_record_bound_and_pending(tmp_path: Path) -> None:
    async def cancel(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    client = Uploader("https://examen.test", "secret", transport=httpx.MockTransport(cancel))
    save_run(tmp_path, payload())
    with pytest.raises(asyncio.CancelledError):
        await client.upload(tmp_path)
    record = records(tmp_path)[0]
    assert not record["uploaded"]
    assert record["destination"] == "https://examen.test"
    assert (await uploader(Server()).upload(tmp_path)).uploaded == 1


class Input(BaseModel):
    value: int


class Output(BaseModel):
    value: int


async def test_upload_outage_cannot_interrupt_or_repeat_paid_benchmark(tmp_path: Path) -> None:
    bench = AsyncBench(Ref(key="p", name="P"), Ref(key="b", name="B"))
    executed: list[int] = []

    @bench.experiment[Input, Output](
        key="experiment",
        name="Experiment",
        cases=[
            Case[Input, Output](
                key="case", name="Case", input=Input(value=1), output=Output(value=1), repeats=8
            )
        ],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    async def experiment(input: Input) -> Output:
        executed.append(input.value)
        await asyncio.sleep(0)
        return Output(value=input.value)

    run = await bench.run({"model": "expensive"}, concurrency=3, output_dir=tmp_path)
    assert len(executed) == 8
    saved = records(run.path / "runs")
    assert len(saved) == 8
    assert all(item["payload"]["metrics"][0]["value"] == 1 for item in saved)
    assert run.completed == 8 and run.status == "completed"
    assert (await uploader(Server(failures=100), max_attempts=2).upload(run.path)).pending == 8
    assert (await uploader(Server()).upload(tmp_path)).uploaded == 8
    assert len(executed) == 8


async def test_old_backend_acknowledgement_stays_pending(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def old_server(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"run": {"id": "server-generated-id"}})

    client = Uploader("https://examen.test", "secret", transport=httpx.MockTransport(old_server))
    path = save_run(tmp_path, payload())
    assert (await client.upload(path)).pending == 1
    assert len(requests) == 1
    assert not records(tmp_path)[0]["uploaded"]


def test_unwritable_store_fails(tmp_path: Path) -> None:
    directory = tmp_path / "not-a-directory"
    directory.write_text("existing file")
    with pytest.raises(OSError):
        save_run(directory, payload())
    assert directory.read_text() == "existing file"


@pytest.mark.parametrize("path", ["missing", "."])
async def test_upload_rejects_missing_or_empty_artifacts(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError):
        await uploader(Server()).upload(tmp_path / path)
