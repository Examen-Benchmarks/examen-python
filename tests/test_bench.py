"""End-to-end behavioral tests for AsyncBench.

Uses an in-memory FakeBackend (no live HTTP). Covers the contract that matters
to downstream users: success/error status, repeats, DI overrides, scorer
type-mismatch validation at decoration.
"""

from typing import Any

import pytest
from pydantic import BaseModel

from examen import (
    AsyncBench,
    Case,
    Depends,
    ExactMatchScorer,
    Trace,
)


class FakeBackend:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.close_calls = 0

    async def ingest_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"ok": True}

    async def close(self) -> None:
        self.close_calls += 1


class Input(BaseModel):
    a: int
    b: int


class Output(BaseModel):
    result: int


class Dep:
    def __init__(self, label: str) -> None:
        self.label = label


def make_dep() -> Dep:
    return Dep("default")


async def test_succeeded_run_with_matching_output_emits_metric_one() -> None:
    backend = FakeBackend()
    bench = AsyncBench(backends=[backend], project_name="p", name="b")

    @bench.experiment[Input, Output](
        name="add",
        cases=[Case[Input, Output](name="ok", input=Input(a=1, b=2), output=Output(result=3))],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def add(input: Input, trace: Trace[Input, Output]) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"})

    assert len(backend.payloads) == 1
    p = backend.payloads[0]
    assert p["run"]["status"] == "succeeded"
    assert p["metrics"] == [{"name": "exact_match", "kind": "ratio", "value": 1.0, "context": None}]


async def test_mismatched_output_emits_metric_zero() -> None:
    backend = FakeBackend()
    bench = AsyncBench(backends=[backend], project_name="p", name="b")

    @bench.experiment[Input, Output](
        name="add",
        cases=[Case[Input, Output](name="bad", input=Input(a=1, b=2), output=Output(result=99))],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def add(input: Input, trace: Trace[Input, Output]) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"})

    assert backend.payloads[0]["metrics"][0]["value"] == 0.0


async def test_repeats_produce_separate_runs() -> None:
    backend = FakeBackend()
    bench = AsyncBench(backends=[backend], project_name="p", name="b")

    @bench.experiment[Input, Output](
        name="add",
        cases=[
            Case[Input, Output](name="r", input=Input(a=1, b=2), output=Output(result=3), repeats=3)
        ],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def add(input: Input, trace: Trace[Input, Output]) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"})

    assert len(backend.payloads) == 3


async def test_function_raise_marks_run_errored() -> None:
    backend = FakeBackend()
    bench = AsyncBench(backends=[backend], project_name="p", name="b")

    @bench.experiment[Input, Output](
        name="boom",
        cases=[Case[Input, Output](name="x", input=Input(a=0, b=0), output=Output(result=0))],
        scorers=[],
    )
    async def boom(input: Input) -> Output:
        raise RuntimeError("kaboom")

    await bench.run(version={"v": "1"})

    p = backend.payloads[0]
    assert p["run"]["status"] == "errored"
    assert "kaboom" in p["run"]["error_message"]
    assert p["metrics"] == []


async def test_dependency_override_is_applied() -> None:
    backend = FakeBackend()
    bench = AsyncBench(backends=[backend], project_name="p", name="b")

    @bench.experiment[Input, Output](
        name="dep",
        cases=[Case[Input, Output](name="x", input=Input(a=1, b=2), output=Output(result=3))],
        scorers=[],
    )
    def f(input: Input, trace: Trace[Input, Output], dep: Dep = Depends(make_dep)) -> Output:
        trace.step("seen", dep_label=dep.label)
        return Output(result=input.a + input.b)

    await bench.run(
        version={"v": "1"},
        dependency_overrides={make_dep: lambda: Dep("overridden")},
    )

    steps = backend.payloads[0]["run"]["trace"]["steps"]
    assert steps[0]["fields"]["dep_label"] == "overridden"


def test_scorer_type_mismatch_raises_at_decoration() -> None:
    bench = AsyncBench(backends=[FakeBackend()], project_name="p", name="b")

    class OtherIn(BaseModel):
        x: int

    class OtherOut(BaseModel):
        y: int

    with pytest.raises(TypeError, match="input_type"):

        @bench.experiment[Input, Output](
            name="bad",
            cases=[],
            scorers=[ExactMatchScorer[OtherIn, OtherOut]()],  # type: ignore[list-item]
        )
        def f(input: Input, trace: Trace[Input, Output]) -> Output:
            return Output(result=0)


def test_fans_out_to_all_backends() -> None:
    import asyncio

    b1 = FakeBackend()
    b2 = FakeBackend()
    bench = AsyncBench(backends=[b1, b2], project_name="p", name="b")

    @bench.experiment[Input, Output](
        name="add",
        cases=[Case[Input, Output](name="x", input=Input(a=1, b=2), output=Output(result=3))],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def add(input: Input, trace: Trace[Input, Output]) -> Output:
        return Output(result=input.a + input.b)

    asyncio.run(bench.run(version={"v": "1"}))

    assert len(b1.payloads) == 1
    assert len(b2.payloads) == 1


async def test_no_summarizers_means_null_summaries() -> None:
    backend = FakeBackend()
    bench = AsyncBench(backends=[backend], project_name="p", name="b")

    @bench.experiment[Input, Output](
        name="add",
        cases=[Case[Input, Output](name="x", input=Input(a=1, b=2), output=Output(result=3))],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def add(input: Input, trace: Trace[Input, Output]) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"})

    p = backend.payloads[0]
    assert p["case"]["input_summary"] is None
    assert p["run"]["output_summary"] is None


async def test_summarizers_appear_in_payload() -> None:
    backend = FakeBackend()
    bench = AsyncBench(backends=[backend], project_name="p", name="b")

    @bench.experiment[Input, Output](
        name="add",
        cases=[Case[Input, Output](name="x", input=Input(a=1, b=2), output=Output(result=3))],
        scorers=[ExactMatchScorer[Input, Output]()],
        summarize_input=lambda i: f"{i.a} + {i.b}",
        summarize_output=lambda o: str(o.result),
    )
    def add(input: Input, trace: Trace[Input, Output]) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"})

    p = backend.payloads[0]
    assert p["case"]["input_summary"] == "1 + 2"
    assert p["run"]["output_summary"] == "3"


async def test_summarizer_raise_falls_back_to_repr() -> None:
    backend = FakeBackend()
    bench = AsyncBench(backends=[backend], project_name="p", name="b")

    def boom(_: Input) -> str:
        raise ValueError("nope")

    @bench.experiment[Input, Output](
        name="add",
        cases=[Case[Input, Output](name="x", input=Input(a=1, b=2), output=Output(result=3))],
        scorers=[ExactMatchScorer[Input, Output]()],
        summarize_input=boom,
    )
    def add(input: Input, trace: Trace[Input, Output]) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"})

    p = backend.payloads[0]
    assert p["case"]["input_summary"] == repr(Input(a=1, b=2))


async def test_errored_run_skips_output_summary() -> None:
    backend = FakeBackend()
    bench = AsyncBench(backends=[backend], project_name="p", name="b")

    summarize_output_calls: list[Any] = []

    def track(o: Output) -> str:
        summarize_output_calls.append(o)
        return "called"

    @bench.experiment[Input, Output](
        name="boom",
        cases=[Case[Input, Output](name="x", input=Input(a=0, b=0), output=Output(result=0))],
        scorers=[],
        summarize_input=lambda i: f"{i.a},{i.b}",
        summarize_output=track,
    )
    def boom(input: Input) -> Output:
        raise RuntimeError("kaboom")

    await bench.run(version={"v": "1"})

    p = backend.payloads[0]
    assert p["run"]["status"] == "errored"
    assert p["case"]["input_summary"] == "0,0"  # input summary still computed
    assert p["run"]["output_summary"] is None
    assert summarize_output_calls == []  # summarize_output not invoked on error


def test_non_callable_summarizer_raises_at_decoration() -> None:
    bench = AsyncBench(backends=[FakeBackend()], project_name="p", name="b")

    with pytest.raises(TypeError, match="summarize_input must be callable"):
        bench.experiment[Input, Output](
            name="bad",
            cases=[],
            scorers=[],
            summarize_input="not a function",  # type: ignore[arg-type]
        )


def test_experiment_without_subscript_is_not_callable() -> None:
    """The untyped form is deliberately removed — calling without subscript fails."""
    bench = AsyncBench(backends=[FakeBackend()], project_name="p", name="b")

    with pytest.raises(TypeError, match="not callable"):
        bench.experiment(name="x", cases=[], scorers=[])  # type: ignore[operator]
