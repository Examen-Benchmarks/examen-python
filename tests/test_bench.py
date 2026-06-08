"""End-to-end behavioral tests for AsyncBench.

Uses an in-memory FakeBackend (no live HTTP). Covers the contract that matters
to downstream users: success/error status, repeats, DI overrides, scorer
type-mismatch validation at decoration, keyed identity, and the emitted
collection_path for nested collections.
"""

from typing import Any

import pytest
from pydantic import BaseModel

from examen import (
    AsyncBench,
    AsyncScorer,
    Case,
    Collection,
    Depends,
    ExactMatchScorer,
    Metric,
    MetricKind,
    Ref,
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


def make_bench(backend: FakeBackend) -> AsyncBench:
    return AsyncBench(
        backends=[backend],
        project=Ref(key="p", name="Project"),
        bench=Ref(key="b", name="Bench"),
    )


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
    bench = make_bench(backend)

    @bench.experiment[Input, Output](
        key="add",
        name="add",
        cases=[
            Case[Input, Output](key="ok", name="ok", input=Input(a=1, b=2), output=Output(result=3))
        ],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def add(input: Input, trace: Trace[Input, Output]) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"})

    assert len(backend.payloads) == 1
    p = backend.payloads[0]
    assert p["run"]["status"] == "succeeded"
    assert p["project"] == {"key": "p", "name": "Project", "description": None}
    assert p["bench"] == {"key": "b", "name": "Bench", "description": None}
    assert p["collection_path"] == []
    assert p["experiment"] == {"key": "add", "name": "add", "description": None}
    assert p["case"]["key"] == "ok"
    assert p["metrics"] == [
        {
            "key": "exact_match",
            "name": "exact_match",
            "kind": "ratio",
            "value": 1.0,
            "context": None,
            "description": None,
        }
    ]


async def test_mismatched_output_emits_metric_zero() -> None:
    backend = FakeBackend()
    bench = make_bench(backend)

    @bench.experiment[Input, Output](
        key="add",
        name="add",
        cases=[
            Case[Input, Output](
                key="bad", name="bad", input=Input(a=1, b=2), output=Output(result=99)
            )
        ],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def add(input: Input, trace: Trace[Input, Output]) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"})

    assert backend.payloads[0]["metrics"][0]["value"] == 0.0


async def test_repeats_produce_separate_runs() -> None:
    backend = FakeBackend()
    bench = make_bench(backend)

    @bench.experiment[Input, Output](
        key="add",
        name="add",
        cases=[
            Case[Input, Output](
                key="r", name="r", input=Input(a=1, b=2), output=Output(result=3), repeats=3
            )
        ],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def add(input: Input, trace: Trace[Input, Output]) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"})

    assert len(backend.payloads) == 3


async def test_function_raise_marks_run_errored() -> None:
    backend = FakeBackend()
    bench = make_bench(backend)

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

    await bench.run(version={"v": "1"})

    p = backend.payloads[0]
    assert p["run"]["status"] == "errored"
    assert "kaboom" in p["run"]["error_message"]
    assert p["metrics"] == []


async def test_dependency_override_is_applied() -> None:
    backend = FakeBackend()
    bench = make_bench(backend)

    @bench.experiment[Input, Output](
        key="dep",
        name="dep",
        cases=[
            Case[Input, Output](key="x", name="x", input=Input(a=1, b=2), output=Output(result=3))
        ],
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
    bench = make_bench(FakeBackend())

    class OtherIn(BaseModel):
        x: int

    class OtherOut(BaseModel):
        y: int

    with pytest.raises(TypeError, match="input_type"):

        @bench.experiment[Input, Output](
            key="bad",
            name="bad",
            cases=[],
            scorers=[ExactMatchScorer[OtherIn, OtherOut]()],  # type: ignore[list-item]
        )
        def f(input: Input, trace: Trace[Input, Output]) -> Output:
            return Output(result=0)


def test_duplicate_experiment_key_raises() -> None:
    bench = make_bench(FakeBackend())

    @bench.experiment[Input, Output](key="dup", name="first", cases=[], scorers=[])
    def f(input: Input, trace: Trace[Input, Output]) -> Output:
        return Output(result=0)

    with pytest.raises(ValueError, match="already registered"):

        @bench.experiment[Input, Output](key="dup", name="second", cases=[], scorers=[])
        def g(input: Input, trace: Trace[Input, Output]) -> Output:
            return Output(result=0)


def test_fans_out_to_all_backends() -> None:
    import asyncio

    b1 = FakeBackend()
    b2 = FakeBackend()
    bench = AsyncBench(
        backends=[b1, b2],
        project=Ref(key="p", name="Project"),
        bench=Ref(key="b", name="Bench"),
    )

    @bench.experiment[Input, Output](
        key="add",
        name="add",
        cases=[
            Case[Input, Output](key="x", name="x", input=Input(a=1, b=2), output=Output(result=3))
        ],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def add(input: Input, trace: Trace[Input, Output]) -> Output:
        return Output(result=input.a + input.b)

    asyncio.run(bench.run(version={"v": "1"}))

    assert len(b1.payloads) == 1
    assert len(b2.payloads) == 1


async def test_nested_collections_emit_collection_path() -> None:
    backend = FakeBackend()
    bench = make_bench(backend)

    regression = Collection(key="regression", name="Regression")
    translation = Collection(key="translation", name="Translation")
    regression.include(translation)
    bench.include(regression)

    # Root-mounted experiment.
    @bench.experiment[Input, Output](
        key="smoke",
        name="smoke",
        cases=[
            Case[Input, Output](key="s", name="s", input=Input(a=1, b=1), output=Output(result=2))
        ],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def smoke(input: Input) -> Output:
        return Output(result=input.a + input.b)

    # Experiment two levels deep: regression → translation.
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

    await bench.run(version={"v": "1"})

    by_exp = {p["experiment"]["key"]: p for p in backend.payloads}
    assert by_exp["smoke"]["collection_path"] == []
    assert by_exp["bleu"]["collection_path"] == [
        {"key": "regression", "name": "Regression", "description": None},
        {"key": "translation", "name": "Translation", "description": None},
    ]


def test_duplicate_collection_key_raises() -> None:
    bench = make_bench(FakeBackend())
    bench.include(Collection(key="dup", name="First"))

    with pytest.raises(ValueError, match="already included"):
        bench.include(Collection(key="dup", name="Second"))


async def test_same_experiment_key_under_different_collections_coexist() -> None:
    """Experiment identity is sibling-scoped: the same key may live under two
    different collections without colliding (benches.md invariant)."""
    backend = FakeBackend()
    bench = make_bench(backend)

    fast = Collection(key="fast", name="Fast")
    slow = Collection(key="slow", name="Slow")
    bench.include(fast)
    bench.include(slow)

    @fast.experiment[Input, Output](
        key="accuracy",
        name="Accuracy",
        cases=[
            Case[Input, Output](key="c", name="c", input=Input(a=1, b=1), output=Output(result=2))
        ],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def fast_acc(input: Input) -> Output:
        return Output(result=input.a + input.b)

    @slow.experiment[Input, Output](
        key="accuracy",  # same key, different parent — allowed
        name="Accuracy",
        cases=[
            Case[Input, Output](key="c", name="c", input=Input(a=2, b=2), output=Output(result=4))
        ],
        scorers=[ExactMatchScorer[Input, Output]()],
    )
    def slow_acc(input: Input) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"})

    assert len(backend.payloads) == 2
    paths = {
        tuple(seg["key"] for seg in p["collection_path"])
        for p in backend.payloads
        if p["experiment"]["key"] == "accuracy"
    }
    assert paths == {("fast",), ("slow",)}


async def test_descriptions_flow_into_payload() -> None:
    """key + name + description (D15) are emitted for every named entity, and
    the collection_path segments carry their descriptions too."""
    backend = FakeBackend()
    bench = AsyncBench(
        backends=[backend],
        project=Ref(key="p", name="Project", description="the project"),
        bench=Ref(key="b", name="Bench", description="the bench"),
    )
    group = Collection(key="g", name="Group", description="a group")
    bench.include(group)

    @group.experiment[Input, Output](
        key="add",
        name="Add",
        description="adds two ints",
        cases=[
            Case[Input, Output](
                key="x",
                name="X",
                description="a case",
                input=Input(a=1, b=2),
                output=Output(result=3),
            )
        ],
        scorers=[],
    )
    def add(input: Input) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"})

    p = backend.payloads[0]
    assert p["project"]["description"] == "the project"
    assert p["bench"]["description"] == "the bench"
    assert p["collection_path"] == [{"key": "g", "name": "Group", "description": "a group"}]
    assert p["experiment"]["description"] == "adds two ints"
    assert p["case"]["description"] == "a case"


async def test_scorer_emits_multiple_metrics_with_distinct_keys() -> None:
    backend = FakeBackend()
    bench = make_bench(backend)

    class MultiScorer(AsyncScorer[Input, Output]):
        async def score(
            self,
            case: Case[Input, Output],
            trace: Trace[Input, Output],
        ) -> list[Metric]:
            return [
                Metric(key="count", name="Count", kind=MetricKind.COUNT, value=2.0),
                Metric(key="ratio", name="Ratio", kind=MetricKind.RATIO, value=0.5),
            ]

    @bench.experiment[Input, Output](
        key="add",
        name="add",
        cases=[
            Case[Input, Output](key="x", name="x", input=Input(a=1, b=2), output=Output(result=3))
        ],
        scorers=[MultiScorer()],
    )
    def add(input: Input) -> Output:
        return Output(result=input.a + input.b)

    await bench.run(version={"v": "1"})

    metrics = backend.payloads[0]["metrics"]
    assert [m["key"] for m in metrics] == ["count", "ratio"]
    assert [m["value"] for m in metrics] == [2.0, 0.5]


def _tree_bench(backend: FakeBackend) -> AsyncBench:
    """A 3-experiment tree:

    bench
    ├── smoke                       (root)
    └── regression
        ├── latency
        └── translation
            └── bleu
    """
    bench = make_bench(backend)

    regression = Collection(key="regression", name="Regression")
    translation = Collection(key="translation", name="Translation")
    regression.include(translation)
    bench.include(regression)

    @bench.experiment[Input, Output](
        key="smoke",
        name="smoke",
        cases=[
            Case[Input, Output](key="s", name="s", input=Input(a=1, b=1), output=Output(result=2))
        ],
        scorers=[],
    )
    def smoke(input: Input) -> Output:
        return Output(result=input.a + input.b)

    @regression.experiment[Input, Output](
        key="latency",
        name="latency",
        cases=[
            Case[Input, Output](key="l", name="l", input=Input(a=1, b=2), output=Output(result=3))
        ],
        scorers=[],
    )
    def latency(input: Input) -> Output:
        return Output(result=input.a + input.b)

    @translation.experiment[Input, Output](
        key="bleu",
        name="BLEU",
        cases=[
            Case[Input, Output](key="t", name="t", input=Input(a=2, b=3), output=Output(result=5))
        ],
        scorers=[],
    )
    def bleu(input: Input) -> Output:
        return Output(result=input.a + input.b)

    return bench


def _ran(backend: FakeBackend) -> set[str]:
    return {p["experiment"]["key"] for p in backend.payloads}


async def test_select_none_runs_everything() -> None:
    backend = FakeBackend()
    bench = _tree_bench(backend)
    await bench.run(version={"v": "1"})
    assert _ran(backend) == {"smoke", "latency", "bleu"}


async def test_select_single_root_experiment() -> None:
    backend = FakeBackend()
    bench = _tree_bench(backend)
    await bench.run(version={"v": "1"}, select="smoke")
    assert _ran(backend) == {"smoke"}


async def test_select_collection_runs_subtree() -> None:
    backend = FakeBackend()
    bench = _tree_bench(backend)
    await bench.run(version={"v": "1"}, select="regression")
    assert _ran(backend) == {"latency", "bleu"}


async def test_select_nested_collection_runs_its_subtree() -> None:
    backend = FakeBackend()
    bench = _tree_bench(backend)
    await bench.run(version={"v": "1"}, select="regression/translation")
    assert _ran(backend) == {"bleu"}


async def test_select_full_path_runs_single_nested_experiment() -> None:
    backend = FakeBackend()
    bench = _tree_bench(backend)
    await bench.run(version={"v": "1"}, select="regression/translation/bleu")
    assert _ran(backend) == {"bleu"}


async def test_select_accepts_multiple_selectors() -> None:
    backend = FakeBackend()
    bench = _tree_bench(backend)
    await bench.run(version={"v": "1"}, select=["smoke", "regression/translation"])
    assert _ran(backend) == {"smoke", "bleu"}


async def test_select_unknown_raises_and_touches_no_backend() -> None:
    backend = FakeBackend()
    bench = _tree_bench(backend)
    with pytest.raises(ValueError, match="matched no experiments"):
        await bench.run(version={"v": "1"}, select="nope")
    # Validation happens before any backend is touched: no runs, no close().
    assert backend.payloads == []
    assert backend.close_calls == 0


async def test_select_partial_unknown_in_list_raises() -> None:
    backend = FakeBackend()
    bench = _tree_bench(backend)
    with pytest.raises(ValueError, match="regression/typo"):
        await bench.run(version={"v": "1"}, select=["smoke", "regression/typo"])
    assert backend.payloads == []


async def test_no_summarizers_means_null_summaries() -> None:
    backend = FakeBackend()
    bench = make_bench(backend)

    @bench.experiment[Input, Output](
        key="add",
        name="add",
        cases=[
            Case[Input, Output](key="x", name="x", input=Input(a=1, b=2), output=Output(result=3))
        ],
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
    bench = make_bench(backend)

    @bench.experiment[Input, Output](
        key="add",
        name="add",
        cases=[
            Case[Input, Output](key="x", name="x", input=Input(a=1, b=2), output=Output(result=3))
        ],
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
    bench = make_bench(backend)

    def boom(_: Input) -> str:
        raise ValueError("nope")

    @bench.experiment[Input, Output](
        key="add",
        name="add",
        cases=[
            Case[Input, Output](key="x", name="x", input=Input(a=1, b=2), output=Output(result=3))
        ],
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
    bench = make_bench(backend)

    summarize_output_calls: list[Any] = []

    def track(o: Output) -> str:
        summarize_output_calls.append(o)
        return "called"

    @bench.experiment[Input, Output](
        key="boom",
        name="boom",
        cases=[
            Case[Input, Output](key="x", name="x", input=Input(a=0, b=0), output=Output(result=0))
        ],
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
    bench = make_bench(FakeBackend())

    with pytest.raises(TypeError, match="summarize_input must be callable"):
        bench.experiment[Input, Output](
            key="bad",
            name="bad",
            cases=[],
            scorers=[],
            summarize_input="not a function",  # type: ignore[arg-type]
        )


def test_experiment_without_subscript_is_not_callable() -> None:
    """The untyped form is deliberately removed — calling without subscript fails."""
    bench = make_bench(FakeBackend())

    with pytest.raises(TypeError, match="not callable"):
        bench.experiment(key="x", name="x", cases=[], scorers=[])  # type: ignore[operator]
