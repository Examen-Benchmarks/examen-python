"""Shared experiment-hosting machinery for benches and collections.

Both `AsyncBench` (the bench root) and `Collection` (a nestable sub-tree) can
register experiments and include child collections. The FastAPI analogy holds:
the bench is the `app`, a collection is an `APIRouter`, and
`@host.experiment[In, Out](...)` is `@app.get` / `@router.get`.

This module holds everything common to both — the `_Experiment` record, the
mandatory-subscript `experiment` registrar, the type-checking decorator factory,
and the function-signature inspection — so `bench.py` and `collection.py` stay
thin. Sibling-`key` uniqueness (D15) is validated here, at registration/include
time, mirroring the server's two-partial-index rule.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from pydantic import BaseModel

from examen.lib.base import Case, InputT, OutputT, Ref
from examen.lib.depends import DependsMarker
from examen.lib.scorers import AsyncScorer
from examen.lib.trace import Trace

if TYPE_CHECKING:
    from examen.lib.collection import Collection

# Preserves the decorated function's exact type (sync or async) through the
# decorator. Runtime _inspect_func still validates that input/return types
# match the experiment's [Input, Output] subscript.
F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class _Experiment:
    key: str
    name: str
    description: str | None
    func: Callable[..., Any]
    cases: list[Case[Any, Any]]
    scorers: list[AsyncScorer[Any, Any]]
    input_type: type[BaseModel]
    output_type: type[BaseModel | None]
    input_param: str
    trace_param: str | None
    summarize_input: Callable[[Any], str] | None
    summarize_output: Callable[[Any], str] | None


class _TypedExperimentFactory(Generic[InputT, OutputT]):
    """Decorator factory returned by ``host.experiment[Input, Output]``.

    Carries the concrete ``InputT`` / ``OutputT`` through every kwarg so cases,
    scorers, summarizers, and the decorated function are all checked together.
    """

    def __init__(
        self,
        host: _ExperimentHost,
        input_type: type[InputT],
        output_type: type[OutputT],
    ) -> None:
        self._host = host
        self._input_type = input_type
        self._output_type = output_type

    def __call__(
        self,
        *,
        key: str,
        name: str,
        cases: list[Case[InputT, OutputT]],
        scorers: list[AsyncScorer[InputT, OutputT]],
        description: str | None = None,
        summarize_input: Callable[[InputT], str] | None = None,
        summarize_output: Callable[[OutputT], str] | None = None,
    ) -> Callable[[F], F]:
        # OutputT is bound to `BaseModel | None`, so the runtime class is
        # always either a BaseModel subclass or `NoneType`. mypy doesn't bridge
        # `type[OutputT]` to `type[BaseModel] | type[None]` automatically.
        return self._host._register_experiment(
            input_type=self._input_type,
            output_type=cast("type[BaseModel] | type[None]", self._output_type),
            key=key,
            name=name,
            description=description,
            cases=cases,
            scorers=scorers,
            summarize_input=summarize_input,
            summarize_output=summarize_output,
        )


class _ExperimentRegistrar:
    """Subscript-only accessor for ``host.experiment``.

    Single form, mandatory subscript so every kwarg is type-checked::

        @host.experiment[Input, Output](key=..., name=..., cases=..., scorers=..., ...)
        def f(input: Input, ...) -> Output: ...

    There is deliberately no untyped ``host.experiment(...)`` form. Allowing one
    would let downstream users opt out of all type-checking by omitting the
    subscript — silently turning ``cases`` / ``scorers`` / summarizers into
    ``Any`` and defeating the whole point of the generics.
    """

    def __init__(self, host: _ExperimentHost) -> None:
        self._host = host

    def __getitem__(
        self,
        params: tuple[type[InputT], type[OutputT]],
    ) -> _TypedExperimentFactory[InputT, OutputT]:
        if not isinstance(params, tuple) or len(params) != 2:
            raise TypeError("experiment[Input, Output] expects exactly two type parameters")
        input_type, output_type = params
        return _TypedExperimentFactory(self._host, input_type, output_type)


class _ExperimentHost:
    """Base for anything that can hold experiments and child collections.

    Subclassed by `AsyncBench` (the bench root) and `Collection`. Holds the
    `experiment` registrar plus the registered experiments and included child
    collections, keyed by their `key`. Both namespaces enforce sibling-`key`
    uniqueness independently (experiments and collections are separate server
    tables with separate partial indexes, so an experiment and a collection may
    share a key).
    """

    experiment: _ExperimentRegistrar

    def __init__(self) -> None:
        self._experiments: dict[str, _Experiment] = {}
        self._collections: dict[str, Collection] = {}
        self.experiment = _ExperimentRegistrar(self)

    def include(self, collection: Collection) -> None:
        """Mount a child collection under this host (the bench root or a parent collection)."""
        if collection.key in self._collections:
            raise ValueError(f"Collection {collection.key!r} already included under this parent")
        self._collections[collection.key] = collection

    def _register_experiment(
        self,
        *,
        input_type: type[BaseModel],
        output_type: type[BaseModel | None],
        key: str,
        name: str,
        description: str | None,
        cases: list[Case[Any, Any]],
        scorers: list[AsyncScorer[Any, Any]],
        summarize_input: Callable[[Any], str] | None,
        summarize_output: Callable[[Any], str] | None,
    ) -> Callable[[F], F]:
        if summarize_input is not None and not callable(summarize_input):
            raise TypeError("summarize_input must be callable")
        if summarize_output is not None and not callable(summarize_output):
            raise TypeError("summarize_output must be callable")

        def decorator(func: F) -> F:
            input_param, trace_param, sig_input_type, sig_output_type = _inspect_func(func)

            if input_type is not sig_input_type:
                raise TypeError(
                    f"@host.experiment[{input_type.__name__}, ...] doesn't match "
                    f"{func.__name__}'s input parameter type {sig_input_type.__name__}"
                )
            if output_type is not sig_output_type:
                raise TypeError(
                    f"@host.experiment[..., {output_type.__name__}] doesn't match "
                    f"{func.__name__}'s return type {sig_output_type.__name__}"
                )

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

            if key in self._experiments:
                raise ValueError(f"Experiment {key!r} already registered under this parent")

            self._experiments[key] = _Experiment(
                key=key,
                name=name,
                description=description,
                func=func,
                cases=cases,
                scorers=scorers,
                input_type=input_type,
                output_type=output_type,
                input_param=input_param,
                trace_param=trace_param,
                summarize_input=summarize_input,
                summarize_output=summarize_output,
            )
            return func

        return decorator

    def _iter_experiments(
        self,
        path: list[Ref],
    ) -> Iterator[tuple[_Experiment, list[Ref]]]:
        """Yield every (experiment, collection_path) pair in this subtree.

        Root-mounted experiments come first with the host's own ``path``; then
        each child collection's subtree, with its ``{key, name, description}``
        segment appended. ``path`` is ``[]`` at the bench root.
        """
        for exp in self._experiments.values():
            yield exp, path
        for coll in self._collections.values():
            segment = Ref(key=coll.key, name=coll.name, description=coll.description)
            yield from coll._iter_experiments([*path, segment])


def _is_trace(ann: Any) -> bool:
    if ann is Trace:
        return True
    if typing.get_origin(ann) is Trace:
        return True
    return isinstance(ann, type) and issubclass(ann, Trace)


def _inspect_func(
    func: Callable[..., Any],
) -> tuple[str, str | None, type[BaseModel], type[BaseModel | None]]:
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
