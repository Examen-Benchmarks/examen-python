"""Scorers: grade runs against their traces, emit metrics.

Scorers are generic over `Input` and `Output` and parametrized at instantiation
(`ExactMatchScorer[Input, Output]()`) or in the subclass definition. The runner
validates at decoration time that an experiment's scorers match its function
signature, so a mismatch fails loud at import.

Scorers run after the experiment function, against the stored `Trace` — never
inline. This separation is what enables retroactive re-scoring against
historical runs.

The hierarchy mirrors the bench split: `Scorer` is the common base (just type
validation); `AsyncScorer` and (later) `SyncScorer` define the actual scoring
contract. An `AsyncBench` accepts only `AsyncScorer` instances; a `SyncBench`
will accept only `SyncScorer` instances; a future smart `Bench` will accept
either.
"""

import typing
from abc import ABC, abstractmethod
from typing import Generic, cast

from pydantic import BaseModel

from examen.lib.base import Case, InputT, Metric, MetricKind, OutputT
from examen.lib.trace import Trace


def _is_concrete_param(a: object) -> bool:
    """Validate a type argument used to bind a scorer's `[Input, Output]`.

    Accepts BaseModel subclasses and `NoneType` (the latter is needed because
    `OutputT` is bound to `BaseModel | None` so `Scorer[Input, None]` is legal).
    Union types are intentionally rejected — the runner compares parameters by
    identity, so each scorer must bind to a single concrete type.
    """
    return isinstance(a, type) and (issubclass(a, BaseModel) or a is type(None))


class Scorer(Generic[InputT, OutputT]):
    """Common scorer base. Holds the type-validation surface shared by all variants.

    Do not subclass `Scorer` directly — extend `AsyncScorer` (today) or
    `SyncScorer` (planned). Instances of plain `Scorer` cannot score anything;
    the actual `score()` contract lives on the variant subclasses.

    Two valid type-binding patterns for variant subclasses:

    1. Subscript at instantiation (typical for built-ins)::

           ExactMatchScorer[MyInput, MyOutput]()

    2. Bind in the subclass (typical for one-off custom scorers)::

           class MyScorer(AsyncScorer[MyInput, MyOutput]):
               async def score(self, case, trace) -> Metric: ...
           MyScorer()
    """

    @property
    def input_type(self) -> type[InputT]:
        return self._params()[0]

    @property
    def output_type(self) -> type[OutputT]:
        return self._params()[1]

    def _params(self) -> tuple[type[InputT], type[OutputT]]:
        # Pattern 1: subscripted at instantiation, e.g. ExactMatchScorer[I, O]()
        orig = getattr(self, "__orig_class__", None)
        if orig is not None:
            args = typing.get_args(orig)
            if len(args) == 2 and all(isinstance(a, type) for a in args):
                return cast("tuple[type[InputT], type[OutputT]]", args)

        # Pattern 2: subscripted in subclass, e.g. class MyScorer(AsyncScorer[I, O])
        for base in getattr(type(self), "__orig_bases__", ()):
            origin = typing.get_origin(base)
            if origin is None or not isinstance(origin, type):
                continue
            if not issubclass(origin, Scorer):
                continue
            args = typing.get_args(base)
            if len(args) == 2 and all(_is_concrete_param(a) for a in args):
                return cast("tuple[type[InputT], type[OutputT]]", args)

        raise TypeError(
            f"{type(self).__name__} has no concrete type parameters. Either "
            f"instantiate as {type(self).__name__}[Input, Output]() or define "
            f"the subclass with concrete types: class MyScorer(AsyncScorer[Input, Output])."
        )


class AsyncScorer(Scorer[InputT, OutputT], ABC):
    """Async scorer — `score()` is awaitable.

    Use this when scoring involves I/O (LLM calls, HTTP, DB) or when you want a
    consistent contract for async benches. For pure CPU work, the overhead of
    `async def` is negligible.

    A single scoring pass can emit multiple metrics — return the full list. A
    scorer that produces one metric returns a one-element list. An empty list
    is allowed (e.g. a guard scorer that only reports on certain conditions).
    Metric names must be unique within an experiment.
    """

    @abstractmethod
    async def score(
        self,
        case: Case[InputT, OutputT],
        trace: Trace[InputT, OutputT],
    ) -> list[Metric]: ...


class ExactMatchScorer(AsyncScorer[InputT, OutputT]):
    async def score(
        self,
        case: Case[InputT, OutputT],
        trace: Trace[InputT, OutputT],
    ) -> list[Metric]:
        match = trace.output == case.output
        return [
            Metric(
                name="exact_match",
                kind=MetricKind.RATIO,
                value=1.0 if match else 0.0,
            )
        ]


class LLMAsAJudgeScorer(AsyncScorer[InputT, OutputT]):
    def __init__(self, model: str, guidelines: str) -> None:
        self.model = model
        self.guidelines = guidelines

    async def score(
        self,
        case: Case[InputT, OutputT],
        trace: Trace[InputT, OutputT],
    ) -> list[Metric]:
        raise NotImplementedError(
            "LLMAsAJudgeScorer is a placeholder; wire up an LLM client to implement."
        )
