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
from typing import Any

from examen.lib.backends.base import Backend
from examen.lib.base import Case, Metric, RunStatus
from examen.lib.depends import DependsMarker, solve
from examen.lib.scorers import Scorer
from examen.lib.trace import Trace


@dataclass
class _Experiment:
    name: str
    func: Callable[..., Any]
    cases: list[Case[Any, Any]]
    scorers: list[Scorer[Any, Any]]
    input_type: type
    output_type: type
    input_param: str
    trace_param: str | None


class AsyncBench:
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

    def experiment(
        self,
        *,
        name: str,
        cases: list[Case[Any, Any]],
        scorers: list[Scorer[Any, Any]],
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            input_param, trace_param, input_type, output_type = _inspect_func(func)

            for scorer in scorers:
                if scorer.input_type is not input_type:
                    raise TypeError(
                        f"Scorer {type(scorer).__name__} has input_type "
                        f"{scorer.input_type!r}, but {func.__name__} takes "
                        f"{input_type!r}"
                    )
                if scorer.output_type is not output_type:
                    raise TypeError(
                        f"Scorer {type(scorer).__name__} has output_type "
                        f"{scorer.output_type!r}, but {func.__name__} returns "
                        f"{output_type!r}"
                    )

            if name in self._experiments:
                raise ValueError(f"Experiment {name!r} already registered")

            self._experiments[name] = _Experiment(
                name=name,
                func=func,
                cases=cases,
                scorers=scorers,
                input_type=input_type,
                output_type=output_type,
                input_param=input_param,
                trace_param=trace_param,
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

        payload = {
            "project": {"name": self.project_name},
            "bench": {"name": self.name},
            "experiment": {"name": exp.name},
            "case": {
                "name": case.name,
                "payload": case.input.model_dump(mode="json"),
            },
            "version": {"components": version},
            "run": {
                "status": status.value,
                "trace": trace.model_dump(mode="json"),
                "started_at": trace.started_at.isoformat() if trace.started_at else None,
                "finished_at": trace.finished_at.isoformat() if trace.finished_at else None,
                "error_message": error_message,
            },
            "metrics": [m.model_dump(mode="json") for m in metrics],
        }

        await asyncio.gather(*(b.ingest_run(payload) for b in self.backends))


def _is_trace(ann: Any) -> bool:
    if ann is Trace:
        return True
    if typing.get_origin(ann) is Trace:
        return True
    return isinstance(ann, type) and issubclass(ann, Trace)


def _inspect_func(func: Callable[..., Any]) -> tuple[str, str | None, type, type]:
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
