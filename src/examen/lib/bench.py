"""`AsyncBench`: register experiments, run them, ship results to backends.

The current implementation is async-only. Sync (`SyncBench`) and an
auto-detecting `Bench` facade are planned but not yet shipped.

A bench is the root of a collection tree (the FastAPI `app`): experiments are
mounted at the root with ``@bench.experiment[In, Out](...)`` or under a
`Collection` included via ``bench.include(collection)``. At ``run()`` the bench
walks every subtree, computes each experiment's ``collection_path``, and fans
the resulting payload out to all backends in parallel. Backends share the same
keyed data model so a run can be recorded locally and remotely at once.
"""

import asyncio
import inspect
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Any

from examen.lib.backends.base import Backend
from examen.lib.base import Case, Metric, Ref, RunStatus
from examen.lib.depends import solve
from examen.lib.host import _Experiment, _ExperimentHost
from examen.lib.trace import Trace


class AsyncBench(_ExperimentHost):
    def __init__(
        self,
        backends: Sequence[Backend],
        project: Ref,
        bench: Ref,
    ) -> None:
        super().__init__()
        # `Sequence[Backend]` (covariant) so callers can pass a concrete
        # `list[LocalReportBackend]` without the type-checker rejecting it
        # under list invariance. We copy into a list to own the storage.
        self.backends: list[Backend] = list(backends)
        self.project = project
        self.bench = bench

    async def run(
        self,
        version: dict[str, str],
        dependency_overrides: dict[Callable[..., Any], Callable[..., Any]] | None = None,
    ) -> None:
        overrides = dependency_overrides or {}
        try:
            for exp, collection_path in self._iter_experiments([]):
                for case in exp.cases:
                    for _ in range(case.repeats):
                        await self._run_one(exp, collection_path, case, version, overrides)
        finally:
            # Backends with end-of-run artifacts (LocalReportBackend writes its
            # HTML here) need a finalize hook. The HTTP Connector's close() is
            # a no-op. Run in finally so artifacts are produced even if a run
            # raised through.
            await asyncio.gather(*(b.close() for b in self.backends))

    async def _run_one(
        self,
        exp: _Experiment,
        collection_path: list[Ref],
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
                    metrics.extend(await scorer.score(case, trace))
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
            "project": self.project.model_dump(mode="json"),
            "bench": self.bench.model_dump(mode="json"),
            # [] = mounted at the bench root; otherwise ordered root-down.
            "collection_path": [seg.model_dump(mode="json") for seg in collection_path],
            "experiment": {"key": exp.key, "name": exp.name, "description": exp.description},
            "case": {
                "key": case.key,
                "name": case.name,
                "description": case.description,
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
