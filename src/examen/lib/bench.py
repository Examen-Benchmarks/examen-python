"""Typed benchmark definitions and execution with durable local JSON results."""

import asyncio
import inspect
import json
import re
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from examen.artifacts import ExperimentInfo, RunProgress, RunResult, save_run, write_record
from examen.lib.base import Case, Metric, Ref, RunStatus
from examen.lib.depends import solve
from examen.lib.host import _Experiment, _ExperimentHost
from examen.lib.trace import Trace


class AsyncBench(_ExperimentHost):
    def __init__(
        self,
        project: Ref,
        bench: Ref,
    ) -> None:
        super().__init__()
        self.project = project
        self.bench = bench

    async def run(
        self,
        version: dict[str, Any],
        dependency_overrides: dict[Callable[..., Any], Callable[..., Any]] | None = None,
        select: str | Sequence[str] | None = None,
        concurrency: int = 15,
        *,
        output_dir: str | Path = ".examen/results",
        filter_pattern: str | None = None,
        on_progress: Callable[[RunProgress], None] | None = None,
        on_run_saved: Callable[[Path], Any] | None = None,
    ) -> RunResult:
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
        check happens before creating any result files.

        ``concurrency`` bounds how many **runs** (one ``f(case)`` for one repeat,
        the leaf unit) execute at once, across all experiments / cases / repeats.
        The default of 15 overlaps the I/O-bound waits typical of eval workloads;
        pass ``concurrency=1`` for fully serial execution. Each run is isolated
        (its own ``Trace`` and dependency scope), so overlap is safe; only the
        order results are saved becomes nondeterministic, which read-time
        aggregation does not depend on.
        """
        if concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {concurrency}")

        overrides = dependency_overrides or {}
        plan = self._plan(select, filter_pattern)
        version = json.loads(json.dumps(version, allow_nan=False))
        # Validate JSON and create the artifact before incurring execution costs.
        execution_id = str(uuid4())
        directory = Path(output_dir).resolve() / execution_id
        result = RunResult(
            execution_id=execution_id,
            path=directory,
            project_key=self.project.key,
            bench_key=self.bench.key,
            version=version,
            total=sum(case.repeats for exp, _ in plan for case in exp.cases),
        )
        directory.mkdir(parents=True, exist_ok=False)
        write_record(directory / "manifest.json", result)
        semaphore = asyncio.Semaphore(concurrency)

        async def _bounded(
            exp: _Experiment, collection_path: list[Ref], case: Case[Any, Any]
        ) -> None:
            async with semaphore:
                payload = await self._run_one(exp, collection_path, case, version, overrides)
                # Save before invoking the caller's callbacks.
                saved_path = save_run(directory / "runs", payload)
                result.completed += 1
                if payload["run"]["status"] == RunStatus.SUCCEEDED:
                    result.succeeded += 1
                else:
                    result.errored += 1
                if on_run_saved is not None:
                    callback_result = on_run_saved(saved_path)
                    if inspect.isawaitable(callback_result):
                        await callback_result
                if on_progress is not None:
                    on_progress(
                        RunProgress(
                            completed=result.completed,
                            total=result.total,
                            succeeded=result.succeeded,
                            errored=result.errored,
                            experiment="/".join([seg.key for seg in collection_path] + [exp.key]),
                            case=case.key,
                        )
                    )

        try:
            # TaskGroup settles all child tasks before the manifest is finalized,
            # including when interrupted or when a local write fails.
            async with asyncio.TaskGroup() as tasks:
                for exp, collection_path in plan:
                    for case in exp.cases:
                        for _ in range(case.repeats):
                            tasks.create_task(_bounded(exp, collection_path, case))
            result.status = "completed"
        except asyncio.CancelledError:
            result.status = "interrupted"
            raise
        except BaseException:
            result.status = "error"
            raise
        finally:
            result.finished_at = datetime.now(UTC)
            write_record(directory / "manifest.json", result)
        return result

    def list_experiments(
        self,
        select: str | Sequence[str] | None = None,
        filter_pattern: str | None = None,
    ) -> list[ExperimentInfo]:
        """Inspect the execution plan without invoking experiments or writing files.

        A regex filter matches project/bench/collection/experiment keys or the
        experiment's display name. ``select`` retains the SDK's prefix selectors.
        """
        return [
            ExperimentInfo(
                path="/".join([seg.key for seg in path] + [exp.key]),
                name=exp.name,
                cases=len(exp.cases),
                runs=sum(c.repeats for c in exp.cases),
            )
            for exp, path in self._plan(select, filter_pattern)
        ]

    def _plan(
        self,
        select: str | Sequence[str] | None,
        filter_pattern: str | None,
    ) -> list[tuple[_Experiment, list[Ref]]]:
        plan = list(self._iter_experiments([]))
        selectors = _parse_selectors(select)
        if selectors is not None:
            plan = _select(plan, selectors)
        if filter_pattern is not None:
            pattern = re.compile(filter_pattern)
            plan = [
                (exp, path)
                for exp, path in plan
                if pattern.search(
                    "/".join(
                        [self.project.key, self.bench.key] + [seg.key for seg in path] + [exp.key]
                    )
                )
                or pattern.search(exp.name)
            ]
        return plan

    async def _run_one(
        self,
        exp: _Experiment,
        collection_path: list[Ref],
        case: Case[Any, Any],
        version: dict[str, Any],
        overrides: dict[Callable[..., Any], Callable[..., Any]],
    ) -> dict[str, Any]:
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
                "id": str(uuid4()),
                "status": status.value,
                "trace": trace.model_dump(mode="json"),
                "started_at": trace.started_at.isoformat() if trace.started_at else None,
                "finished_at": trace.finished_at.isoformat() if trace.finished_at else None,
                "error_message": error_message,
                "output_summary": output_summary,
            },
            "metrics": [m.model_dump(mode="json") for m in metrics],
        }

        return payload


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
