"""Scorers: grade runs against their traces, emit metrics.

Scorers are generic over `Input` and `Output` and parametrized at instantiation
(`ExactMatchScorer[Input, Output]()`). The runner validates at decoration time
that an experiment's scorers match its function signature, so a mismatch fails
loud at import.

Scorers run after the experiment function, against the stored `Trace` — never
inline. This separation is what enables retroactive re-scoring against
historical runs.
"""

import typing
from abc import ABC, abstractmethod
from typing import Generic, cast

from pydantic import BaseModel

from examen.lib.base import Case, InputT, Metric, MetricKind, OutputT
from examen.lib.trace import Trace


class Scorer(ABC, Generic[InputT, OutputT]):
    """Base class for all scorers.

    Subclass and parametrize to bind concrete Input / Output types. Two valid
    patterns:

    1. Subscript at instantiation (typical for built-ins)::

           ExactMatchScorer[MyInput, MyOutput]()

    2. Bind in the subclass (typical for one-off custom scorers)::

           class MyScorer(Scorer[MyInput, MyOutput]):
               def score(self, case, trace) -> Metric: ...
           MyScorer()

    The runner reads `input_type` / `output_type` to validate compatibility
    with the experiment function's signature.
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

        # Pattern 2: subscripted in subclass, e.g. class MyScorer(Scorer[I, O])
        for base in getattr(type(self), "__orig_bases__", ()):
            origin = typing.get_origin(base)
            if origin is None or not isinstance(origin, type):
                continue
            if not issubclass(origin, Scorer):
                continue
            args = typing.get_args(base)
            if len(args) == 2 and all(isinstance(a, type) and issubclass(a, BaseModel) for a in args):
                return cast("tuple[type[InputT], type[OutputT]]", args)

        raise TypeError(
            f"{type(self).__name__} has no concrete type parameters. Either "
            f"instantiate as {type(self).__name__}[Input, Output]() or define "
            f"the subclass with concrete types: class MyScorer(Scorer[Input, Output])."
        )

    @abstractmethod
    def score(
        self,
        case: Case[InputT, OutputT],
        trace: Trace[InputT, OutputT],
    ) -> Metric: ...


class ExactMatchScorer(Scorer[InputT, OutputT]):
    def score(
        self,
        case: Case[InputT, OutputT],
        trace: Trace[InputT, OutputT],
    ) -> Metric:
        if case.output is None:
            raise ValueError("ExactMatchScorer requires Case.output to be set")
        match = trace.output == case.output
        return Metric(
            name="exact_match",
            kind=MetricKind.RATIO,
            value=1.0 if match else 0.0,
        )


class LLMAsaJudgeScorer(Scorer[InputT, OutputT]):
    def __init__(self, model: str, guidelines: str) -> None:
        self.model = model
        self.guidelines = guidelines

    def score(
        self,
        case: Case[InputT, OutputT],
        trace: Trace[InputT, OutputT],
    ) -> Metric:
        raise NotImplementedError(
            "LLMAsaJudgeScorer is a placeholder; wire up an LLM client to implement."
        )
