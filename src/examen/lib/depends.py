"""FastAPI-style dependency injection for experiment functions.

Mark parameters with `Depends(callable)` and the runner resolves them
recursively when the experiment runs. Supports sync and async callables, as
well as (async) generator functions for setup/teardown:

    def db() -> Iterator[DB]:
        conn = open_db()
        try:
            yield conn
        finally:
            conn.close()

Per-run overrides are passed to `bench.run(..., dependency_overrides={...})`,
keyed by the original callable (not by name).
"""

import contextlib
import inspect
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any, TypeVar, cast

T = TypeVar("T")


class DependsMarker:
    """Internal marker placed in default values by `Depends()`.

    Users should never construct this directly — they call `Depends(callable)`,
    which returns a value typed as the callable's return type so the parameter
    stays statically typed, while at runtime placing this marker for the
    runner to detect.
    """

    def __init__(self, dep: Callable[..., Any]) -> None:
        self.dep = dep


def Depends(dep: Callable[..., T]) -> T:
    """Mark a parameter as a dependency resolved by the runner at run time.

    The static return type matches the dep's, so the parameter retains its
    real type for type-checkers and IDEs::

        def f(db: DB = Depends(make_db)) -> Result:
            db  # typed as DB

    At runtime, the value is a `DependsMarker` that the runner replaces with
    the resolved dependency before invoking `f`.
    """
    return cast(T, DependsMarker(dep))


async def solve(
    func: Callable[..., Any],
    overrides: dict[Callable[..., Any], Callable[..., Any]],
    stack: AsyncExitStack,
) -> dict[str, Any]:
    """Resolve all Depends-marked parameters of `func` into kwargs."""
    kwargs: dict[str, Any] = {}
    sig = inspect.signature(func)
    for name, param in sig.parameters.items():
        if isinstance(param.default, DependsMarker):
            dep = overrides.get(param.default.dep, param.default.dep)
            kwargs[name] = await _call_dep(dep, overrides, stack)
    return kwargs


async def _call_dep(
    dep: Callable[..., Any],
    overrides: dict[Callable[..., Any], Callable[..., Any]],
    stack: AsyncExitStack,
) -> Any:
    sub_kwargs = await solve(dep, overrides, stack)

    if inspect.isasyncgenfunction(dep):
        agen = dep(**sub_kwargs)
        value = await agen.__anext__()

        async def _async_cleanup() -> None:
            with contextlib.suppress(StopAsyncIteration):
                await agen.__anext__()

        stack.push_async_callback(_async_cleanup)
        return value

    if inspect.isgeneratorfunction(dep):
        gen = dep(**sub_kwargs)
        value = next(gen)

        def _sync_cleanup() -> None:
            with contextlib.suppress(StopIteration):
                next(gen)

        stack.callback(_sync_cleanup)
        return value

    if inspect.iscoroutinefunction(dep):
        return await dep(**sub_kwargs)

    return dep(**sub_kwargs)
