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
        select: str | Sequence[str] | None = None,
    ) -> None:
        """Run the bench, optionally restricted to part of the tree.

        ``select`` is a pytest-style node selector (or list of them) over the
        experiment's key path — its ``collection_path`` keys plus its own key,
        joined by ``/``. A selector that names a **collection** runs that whole
        subtree; one that names a full path runs a **single experiment**::

            await bench.run(version, select="regression")               # subtree
            await bench.run(version, select="regression/translation/bleu")  # one experiment
            await bench.run(version, select=["smoke", "regression/translation"])

        Matching is by key prefix. ``select=None`` runs everything. A selector
        that matches no experiment raises ``ValueError`` (typo guard), and the
        check happens before any backend is touched so a bad selector produces
        no report.
        """
        overrides = dependency_overrides or {}
        plan = list(self._iter_experiments([]))
        selectors = _parse_selectors(select)
        if selectors is not None:
            plan = _select(plan, selectors)
        try:
            for exp, collection_path in plan:
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


def _parse_selectors(select: str | Sequence[str] | None) -> list[list[str]] | None:
    """Normalise the ``select`` argument into a list of key paths.

    ``None`` (or an empty list) means "no filter — run everything". A string is
    split on ``/`` into key segments; a sequence of strings yields several paths.
    Empty individual selectors (``""``, ``"/"``) are rejected as typos.
    """
    if select is None:
        return None
    raw = [select] if isinstance(select, str) else list(select)
    parsed: list[list[str]] = []
    for sel in raw:
        segments = [seg for seg in sel.split("/") if seg]
        if not segments:
            raise ValueError(f"Invalid empty selector: {sel!r}")
        parsed.append(segments)
    return parsed or None


def _select(
    plan: list[tuple[_Experiment, list[Ref]]],
    selectors: list[list[str]],
) -> list[tuple[_Experiment, list[Ref]]]:
    """Keep only experiments whose key path is prefix-matched by a selector.

    An experiment's key path is its ``collection_path`` keys followed by its own
    key. A selector matches when it equals a prefix of that path, so a collection
    selector pulls in its whole subtree and a full-path selector pulls in exactly
    one experiment. Raises if any selector matched nothing.
    """
    matched = [False] * len(selectors)
    chosen: list[tuple[_Experiment, list[Ref]]] = []
    for exp, collection_path in plan:
        node_keys = [seg.key for seg in collection_path]
        node_keys.append(exp.key)
        keep = False
        for i, segments in enumerate(selectors):
            if node_keys[: len(segments)] == segments:
                matched[i] = True
                keep = True
        if keep:
            chosen.append((exp, collection_path))

    unmatched = ["/".join(selectors[i]) for i, ok in enumerate(matched) if not ok]
    if unmatched:
        raise ValueError(
            f"select matched no experiments: {unmatched}. Use the key path, "
            f"e.g. 'collection/sub/experiment' (collection keys then experiment key)."
        )
    return chosen


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
