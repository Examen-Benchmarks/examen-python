"""`AsyncBench`: register experiments, run them, ship results to backends.

The current implementation is async-only. Sync (`SyncBench`) and an
auto-detecting `Bench` facade are planned but not yet shipped.

The bench owns a list of backends; each completed run is fanned out to all of
them in parallel. Backends share the same data model so a run can be recorded
locally and remotely at once.
"""

import asyncio
import inspect
import typing
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from examen.lib.backends.base import Backend
from examen.lib.base import Case, InputT, Metric, OutputT, RunStatus
from examen.lib.depends import DependsMarker, solve
from examen.lib.scorers import Scorer
from examen.lib.trace import Trace

# Preserves the decorated function's exact type (sync or async) through the
# decorator. Runtime _inspect_func still validates that input/return types
# match the experiment's [Input, Output] subscript.
F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class _Experiment:
    name: str
    func: Callable[..., Any]
    cases: list[Case[Any, Any]]
    scorers: list[Scorer[Any, Any]]
    input_type: type[BaseModel]
    output_type: type[BaseModel]
    input_param: str
    trace_param: str | None
    summarize_input: Callable[[Any], str] | None
    summarize_output: Callable[[Any], str] | None


class _TypedExperimentFactory(Generic[InputT, OutputT]):
    """Decorator factory returned by ``bench.experiment[Input, Output]``.

    Carries the concrete ``InputT`` / ``OutputT`` through every kwarg so cases,
    scorers, summarizers, and the decorated function are all checked together.
    """

    def __init__(
        self,
        bench: "AsyncBench",
        input_type: type[InputT],
        output_type: type[OutputT],
    ) -> None:
        self._bench = bench
        self._input_type = input_type
        self._output_type = output_type

    def __call__(
        self,
        *,
        name: str,
        cases: list[Case[InputT, OutputT]],
        scorers: list[Scorer[InputT, OutputT]],
        summarize_input: Callable[[InputT], str] | None = None,
        summarize_output: Callable[[OutputT], str] | None = None,
    ) -> Callable[[F], F]:
        return self._bench._register_experiment(
            input_type=self._input_type,
            output_type=self._output_type,
            name=name,
            cases=cases,
            scorers=scorers,
            summarize_input=summarize_input,
            summarize_output=summarize_output,
        )


class _ExperimentRegistrar:
    """Subscript-aware accessor for ``AsyncBench.experiment``.

    Two forms::

        @bench.experiment[Input, Output](name=..., cases=..., ...)   # type-checked
        @bench.experiment(name=..., cases=..., ...)                  # untyped fallback

    The subscripted form binds ``InputT`` / ``OutputT`` so every kwarg shares
    the same types — mismatches are caught by the type-checker before runtime.
    The untyped form remains for back-compat and quick scripts.
    """

    def __init__(self, bench: "AsyncBench") -> None:
        self._bench = bench

    def __getitem__(
        self,
        params: tuple[type[InputT], type[OutputT]],
    ) -> _TypedExperimentFactory[InputT, OutputT]:
        if not isinstance(params, tuple) or len(params) != 2:
            raise TypeError(
                "bench.experiment[Input, Output] expects exactly two type parameters"
            )
        input_type, output_type = params
        return _TypedExperimentFactory(self._bench, input_type, output_type)

    def __call__(
        self,
        *,
        name: str,
        cases: list[Case[Any, Any]],
        scorers: list[Scorer[Any, Any]],
        summarize_input: Callable[[Any], str] | None = None,
        summarize_output: Callable[[Any], str] | None = None,
    ) -> Callable[[F], F]:
        return self._bench._register_experiment(
            input_type=None,
            output_type=None,
            name=name,
            cases=cases,
            scorers=scorers,
            summarize_input=summarize_input,
            summarize_output=summarize_output,
        )


class AsyncBench:
    experiment: _ExperimentRegistrar

    def __init__(
        self,
        backends: list[Backend],
        project_name: str,
        name: str,
    ) -> None:
        self.backends = backends
        self.project_name = project_name
        self.name = name
        self._experiments: dict[str, _Experiment] = {}
        self.experiment = _ExperimentRegistrar(self)

    def _register_experiment(
        self,
        *,
        input_type: type[BaseModel] | None,
        output_type: type[BaseModel] | None,
        name: str,
        cases: list[Case[Any, Any]],
        scorers: list[Scorer[Any, Any]],
        summarize_input: Callable[[Any], str] | None,
        summarize_output: Callable[[Any], str] | None,
    ) -> Callable[[F], F]:
        if summarize_input is not None and not callable(summarize_input):
            raise TypeError("summarize_input must be callable")
        if summarize_output is not None and not callable(summarize_output):
            raise TypeError("summarize_output must be callable")

        def decorator(func: F) -> F:
            input_param, trace_param, sig_input_type, sig_output_type = _inspect_func(func)

            # If types were given via subscript, validate the signature matches.
            # When omitted, the signature is the source of truth.
            if input_type is not None and input_type is not sig_input_type:
                raise TypeError(
                    f"@bench.experiment[{input_type.__name__}, ...] doesn't match "
                    f"{func.__name__}'s input parameter type {sig_input_type.__name__}"
                )
            if output_type is not None and output_type is not sig_output_type:
                raise TypeError(
                    f"@bench.experiment[..., {output_type.__name__}] doesn't match "
                    f"{func.__name__}'s return type {sig_output_type.__name__}"
                )

            effective_input = input_type or sig_input_type
            effective_output = output_type or sig_output_type

            for scorer in scorers:
                if scorer.input_type is not effective_input:
                    raise TypeError(
                        f"Scorer {type(scorer).__name__} has input_type "
                        f"{scorer.input_type!r}, but {func.__name__} takes "
                        f"{effective_input!r}"
                    )
                if scorer.output_type is not effective_output:
                    raise TypeError(
                        f"Scorer {type(scorer).__name__} has output_type "
                        f"{scorer.output_type!r}, but {func.__name__} returns "
                        f"{effective_output!r}"
                    )

            if name in self._experiments:
                raise ValueError(f"Experiment {name!r} already registered")

            self._experiments[name] = _Experiment(
                name=name,
                func=func,
                cases=cases,
                scorers=scorers,
                input_type=effective_input,
                output_type=effective_output,
                input_param=input_param,
                trace_param=trace_param,
                summarize_input=summarize_input,
                summarize_output=summarize_output,
            )
            return func

        return decorator

    async def run(
        self,
        version: dict[str, str],
        dependency_overrides: dict[Callable[..., Any], Callable[..., Any]] | None = None,
    ) -> None:
        overrides = dependency_overrides or {}
        for exp in self._experiments.values():
            for case in exp.cases:
                for _ in range(case.repeats):
                    await self._run_one(exp, case, version, overrides)

    async def _run_one(
        self,
        exp: _Experiment,
        case: Case[Any, Any],
        version: dict[str, str],
        overrides: dict[Callable[..., Any], Callable[..., Any]],
    ) -> None:
        trace: Trace[Any, Any] = Trace(
            case_name=case.name,
            input=case.input,
            started_at=datetime.now(UTC),
        )
        status = RunStatus.SUCCEEDED
        error_message: str | None = None

        try:
            async with AsyncExitStack() as stack:
                kwargs = await solve(exp.func, overrides, stack)
                kwargs[exp.input_param] = case.input
                if exp.trace_param is not None:
                    kwargs[exp.trace_param] = trace

                if inspect.iscoroutinefunction(exp.func):
                    output = await exp.func(**kwargs)
                else:
                    output = exp.func(**kwargs)

                trace.output = output
        except Exception as e:
            status = RunStatus.ERRORED
            error_message = f"{type(e).__name__}: {e}"
        finally:
            trace.finished_at = datetime.now(UTC)
            trace.error_message = error_message

        metrics: list[Metric] = []
        if status is RunStatus.SUCCEEDED:
            for scorer in exp.scorers:
                try:
                    metrics.append(scorer.score(case, trace))
                except Exception as e:
                    status = RunStatus.ERRORED
                    error_message = f"Scorer {type(scorer).__name__} raised {type(e).__name__}: {e}"
                    trace.error_message = error_message
                    metrics = []
                    break

        input_summary = _safe_summarize(exp.summarize_input, case.input)
        output_summary: str | None = None
        if status is RunStatus.SUCCEEDED:
            output_summary = _safe_summarize(exp.summarize_output, trace.output)

        payload = {
            "project": {"name": self.project_name},
            "bench": {"name": self.name},
            "experiment": {"name": exp.name},
            "case": {
                "name": case.name,
                "payload": case.input.model_dump(mode="json"),
                "input_summary": input_summary,
            },
            "version": {"components": version},
            "run": {
                "status": status.value,
                "trace": trace.model_dump(mode="json"),
                "started_at": trace.started_at.isoformat() if trace.started_at else None,
                "finished_at": trace.finished_at.isoformat() if trace.finished_at else None,
                "error_message": error_message,
                "output_summary": output_summary,
            },
            "metrics": [m.model_dump(mode="json") for m in metrics],
        }

        await asyncio.gather(*(b.ingest_run(payload) for b in self.backends))


def _safe_summarize(
    fn: Callable[[Any], str] | None,
    value: Any,
) -> str | None:
    """Run a user-supplied summarizer with a repr() fallback.

    Display bugs must not break runs, so a raising summarizer falls back to
    ``repr(value)`` rather than propagating. Returns None when no summarizer
    is supplied or when value is None.
    """
    if fn is None or value is None:
        return None
    try:
        return fn(value)
    except Exception:
        return repr(value)


def _is_trace(ann: Any) -> bool:
    if ann is Trace:
        return True
    if typing.get_origin(ann) is Trace:
        return True
    return isinstance(ann, type) and issubclass(ann, Trace)


def _inspect_func(
    func: Callable[..., Any],
) -> tuple[str, str | None, type[BaseModel], type[BaseModel]]:
    sig = inspect.signature(func)
    hints = typing.get_type_hints(func)

    input_param: str | None = None
    trace_param: str | None = None

    for pname, param in sig.parameters.items():
        if isinstance(param.default, DependsMarker):
            continue
        ann = hints.get(pname, param.annotation)
        if _is_trace(ann):
            trace_param = pname
            continue
        if input_param is None:
            input_param = pname

    if input_param is None:
        raise TypeError(
            f"{func.__name__} must take an input parameter (a non-Depends, non-Trace arg)"
        )

    input_type = hints.get(input_param, sig.parameters[input_param].annotation)
    output_type = hints.get("return", sig.return_annotation)

    if input_type is inspect.Parameter.empty:
        raise TypeError(f"{func.__name__} parameter {input_param!r} must be annotated")
    if output_type is inspect.Signature.empty:
        raise TypeError(f"{func.__name__} must declare a return type annotation")

    return input_param, trace_param, input_type, output_type
