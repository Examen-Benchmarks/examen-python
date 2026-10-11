"""HTML reports rendered from persisted benchmark results."""

import json
from pathlib import Path

from pydantic import BaseModel

from examen import (
    AsyncBench,
    AsyncScorer,
    Case,
    Collection,
    ExactMatchScorer,
    Metric,
    MetricKind,
    Ref,
    Trace,
)
from examen.lib.report import render_html


def write_report(results: Path, destination: Path) -> None:
    runs = [json.loads(path.read_text())["payload"] for path in results.rglob("runs/*.json")]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_html(runs), encoding="utf-8")


class Input(BaseModel):
    a: int
    b: int


class Output(BaseModel):
    result: int


def make_bench(project: str = "proj", bench: str = "bench-x") -> AsyncBench:
    return AsyncBench(
        project=Ref(key="proj", name=project),
        bench=Ref(key="bench", name=bench),
    )


async def test_saved_results_report_writes_self_contained_html(tmp_path: Path) -> None:
    out = tmp_path / "report.html"
    bench = make_bench()

    @bench.experiment[Input, Output](
        key="add",
        name="add",
        cases=[
            Case[Input, Output](
                key="ok", name="ok", input=Input(a=1, b=2), output=Output(result=3)
            ),
            Case[Input, Output](
                key="bad", name="bad", input=Input(a=1, b=2), output=Output(result=99)
            ),
        ],
        scorers=[ExactMatchScorer[Input, Output]()],
        summarize_input=lambda i: f"{i.a} + {i.b}",
        summarize_output=lambda o: str(o.result),
    )
    def add(input: Input, trace: Trace[Input, Output]) -> Output:
        trace.step("compute", a=input.a, b=input.b)
        return Output(result=input.a + input.b)

    await bench.run(version={"sdk": "0.0.3"}, output_dir=tmp_path / "results")
    write_report(tmp_path / "results", out)

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


async def test_saved_results_report_renders_error_runs(tmp_path: Path) -> None:
    out = tmp_path / "report.html"
    bench = make_bench(project="p", bench="b")

    @bench.experiment[Input, Output](
        key="boom",
        name="boom",
        cases=[
            Case[Input, Output](key="x", name="x", input=Input(a=0, b=0), output=Output(result=0))
        ],
        scorers=[],
    )
    async def boom(input: Input) -> Output:
        raise RuntimeError("kaboom")

    await bench.run(version={"v": "1"}, output_dir=tmp_path / "results")
    write_report(tmp_path / "results", out)

    body = out.read_text(encoding="utf-8")
    assert "errored" in body
    assert "kaboom" in body
    assert "RuntimeError" in body


async def test_saved_results_report_creates_parent_dirs(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deeper" / "report.html"
    bench = make_bench(project="p", bench="b")

    @bench.experiment[Input, Output](
        key="add",
        name="add",
        cases=[
            Case[Input, Output](key="x", name="x", input=Input(a=1, b=2), output=Output(result=3))
        ],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def add(input: Input) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"}, output_dir=tmp_path / "results")
    write_report(tmp_path / "results", out)

    assert out.exists()


async def test_nested_collections_render_breadcrumb(tmp_path: Path) -> None:
    out = tmp_path / "report.html"
    bench = make_bench(project="p", bench="b")

    regression = Collection(key="regression", name="Regression")
    translation = Collection(key="translation", name="Translation")
    regression.include(translation)
    bench.include(regression)

    @translation.experiment[Input, Output](
        key="bleu",
        name="BLEU",
        cases=[
            Case[Input, Output](key="t", name="t", input=Input(a=2, b=3), output=Output(result=5))
        ],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def bleu(input: Input) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"}, output_dir=tmp_path / "results")
    write_report(tmp_path / "results", out)

    body = out.read_text(encoding="utf-8")
    assert 'class="breadcrumb"' in body
    assert "Regression" in body
    assert "Translation" in body
    assert "BLEU" in body  # experiment display name


async def test_report_aggregates_by_metric_key_labels_by_name(tmp_path: Path) -> None:
    """Report columns are keyed by the metric *key* (identity) but headed by the
    display *name*; repeats of the same (case, metric key) aggregate into one cell."""
    out = tmp_path / "report.html"

    class NamedScorer(AsyncScorer[Input, Output]):
        async def score(
            self,
            case: Case[Input, Output],
            trace: Trace[Input, Output],
        ) -> list[Metric]:
            return [Metric(key="exact_v2", name="Exact Match", kind=MetricKind.RATIO, value=1.0)]

    bench = make_bench(project="p", bench="b")

    @bench.experiment[Input, Output](
        key="add",
        name="add",
        cases=[
            Case[Input, Output](
                key="x", name="x", input=Input(a=1, b=2), output=Output(result=3), repeats=3
            )
        ],
        scorers=[NamedScorer()],
    )
    def add(input: Input) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"}, output_dir=tmp_path / "results")
    write_report(tmp_path / "results", out)

    body = out.read_text(encoding="utf-8")
    assert "Exact Match" in body  # column header uses the display name
    assert "n=3" in body  # the 3 repeats aggregate under the single metric key


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
                    key="judge",
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

    bench = make_bench(project="p", bench="b")

    @bench.experiment[Input, Output](
        key="add",
        name="add",
        cases=[
            Case[Input, Output](key="x", name="x", input=Input(a=1, b=2), output=Output(result=3))
        ],
        scorers=[JudgeScorer()],
    )
    def add(input: Input) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"}, output_dir=tmp_path / "results")
    write_report(tmp_path / "results", out)

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


async def test_report_combines_saved_executions(tmp_path: Path) -> None:
    out = tmp_path / "report.html"
    bench = make_bench(project="p", bench="b")

    @bench.experiment[Input, Output](
        key="add",
        name="add",
        cases=[
            Case[Input, Output](key="x", name="x", input=Input(a=1, b=2), output=Output(result=3))
        ],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def add(input: Input) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"}, output_dir=tmp_path / "results")
    write_report(tmp_path / "results", out)
    await bench.run(version={"v": "2"}, output_dir=tmp_path / "results")
    write_report(tmp_path / "results", out)

    body = out.read_text(encoding="utf-8")
    # Both saved executions can be combined into one report.
    assert body.count('<details><summary><span class="dot dot-ok"></span>') >= 2
