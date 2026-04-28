"""End-to-end tests for the offline LocalReportBackend.

Runs a tiny bench with the backend wired in, checks that close() writes a
self-contained HTML file containing the expected case names, metric names,
and status indicators.
"""

from pathlib import Path

from pydantic import BaseModel

from examen import (
    AsyncBench,
    AsyncScorer,
    Case,
    ExactMatchScorer,
    LocalReportBackend,
    Metric,
    MetricKind,
    Trace,
)
from examen.lib.report import render_html


class Input(BaseModel):
    a: int
    b: int


class Output(BaseModel):
    result: int


async def test_local_backend_writes_self_contained_html(tmp_path: Path) -> None:
    out = tmp_path / "report.html"
    bench = AsyncBench(
        backends=[LocalReportBackend(out)],
        project_name="proj",
        name="bench-x",
    )

    @bench.experiment[Input, Output](
        name="add",
        cases=[
            Case[Input, Output](name="ok", input=Input(a=1, b=2), output=Output(result=3)),
            Case[Input, Output](name="bad", input=Input(a=1, b=2), output=Output(result=99)),
        ],
        scorers=[ExactMatchScorer[Input, Output]()],
        summarize_input=lambda i: f"{i.a} + {i.b}",
        summarize_output=lambda o: str(o.result),
    )
    def add(input: Input, trace: Trace[Input, Output]) -> Output:
        trace.step("compute", a=input.a, b=input.b)
        return Output(result=input.a + input.b)

    await bench.run(version={"sdk": "0.0.3"})

    assert out.exists()
    body = out.read_text(encoding="utf-8")

    # self-contained: no external resources
    assert "<style>" in body
    assert "<script" not in body
    assert "http://" not in body
    assert "https://" not in body  # except inside <style> none here

    # structural anchors
    assert "<!DOCTYPE html>" in body
    assert "proj" in body
    assert "bench-x" in body
    assert "add" in body  # experiment name
    assert "ok" in body and "bad" in body  # case names
    assert "exact_match" in body  # metric name
    assert "1 + 2" in body  # input_summary rendered


async def test_local_backend_renders_error_runs(tmp_path: Path) -> None:
    out = tmp_path / "report.html"
    bench = AsyncBench(
        backends=[LocalReportBackend(out)],
        project_name="p",
        name="b",
    )

    @bench.experiment[Input, Output](
        name="boom",
        cases=[Case[Input, Output](name="x", input=Input(a=0, b=0), output=Output(result=0))],
        scorers=[],
    )
    async def boom(input: Input) -> Output:
        raise RuntimeError("kaboom")

    await bench.run(version={"v": "1"})

    body = out.read_text(encoding="utf-8")
    assert "errored" in body
    assert "kaboom" in body
    assert "RuntimeError" in body


async def test_local_backend_creates_parent_dirs(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deeper" / "report.html"
    bench = AsyncBench(
        backends=[LocalReportBackend(out)],
        project_name="p",
        name="b",
    )

    @bench.experiment[Input, Output](
        name="add",
        cases=[Case[Input, Output](name="x", input=Input(a=1, b=2), output=Output(result=3))],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def add(input: Input) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"})

    assert out.exists()


async def test_metric_context_renders_under_chip(tmp_path: Path) -> None:
    out = tmp_path / "report.html"

    class JudgeScorer(AsyncScorer[Input, Output]):
        async def score(
            self,
            case: Case[Input, Output],
            trace: Trace[Input, Output],
        ) -> list[Metric]:
            return [
                Metric(
                    name="judge",
                    kind=MetricKind.RATIO,
                    value=0.75,
                    context={
                        "rationale": "off by one — close enough for partial credit",
                        "matched_keys": ["sum"],
                        "model": "gpt-4o-mini",
                    },
                )
            ]

    bench = AsyncBench(
        backends=[LocalReportBackend(out)],
        project_name="p",
        name="b",
    )

    @bench.experiment[Input, Output](
        name="add",
        cases=[Case[Input, Output](name="x", input=Input(a=1, b=2), output=Output(result=3))],
        scorers=[JudgeScorer()],
    )
    def add(input: Input) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"})

    body = out.read_text(encoding="utf-8")
    assert "judge" in body
    assert "metric-context" in body  # the wrapper class is present
    assert "rationale" in body  # the JSON key is rendered
    assert "off by one" in body  # the JSON value is rendered
    assert "gpt-4o-mini" in body


def test_render_html_handles_empty_runs() -> None:
    body = render_html([])
    assert "<!DOCTYPE html>" in body
    assert "No runs" in body


async def test_close_idempotent_across_repeated_run_calls(tmp_path: Path) -> None:
    out = tmp_path / "report.html"
    bench = AsyncBench(
        backends=[LocalReportBackend(out)],
        project_name="p",
        name="b",
    )

    @bench.experiment[Input, Output](
        name="add",
        cases=[Case[Input, Output](name="x", input=Input(a=1, b=2), output=Output(result=3))],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def add(input: Input) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"})
    await bench.run(version={"v": "2"})  # second invocation; close() runs again

    body = out.read_text(encoding="utf-8")
    # Both runs accumulate into the same backend
    assert body.count('<details><summary><span class="dot dot-ok"></span>') >= 2
