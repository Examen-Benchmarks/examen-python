"""Load Python benchmark definitions without executing their experiments."""

import glob
import hashlib
import importlib
import importlib.util
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from examen import AsyncBench

_EXCLUDED = {
    "node_modules",
    "venv",
    "env",
    "site-packages",
    "dist-packages",
    "__pycache__",
    "dist",
    "build",
    "htmlcov",
}


@dataclass(frozen=True)
class DiscoveredBench:
    source: Path
    variable: str
    bench: AsyncBench


@contextmanager
def benchmark_imports(paths: list[Path]) -> Iterator[None]:
    """Keep imports available during discovery and lazy imports during execution."""
    roots = [str(Path.cwd())]
    for path in paths:
        root = path.parent
        roots.append(str(root))
        while (root / "__init__.py").is_file():
            root = root.parent
        roots.append(str(root))
    old_path = sys.path[:]
    sys.path[:0] = list(dict.fromkeys(roots))
    try:
        yield
    finally:
        sys.path[:] = old_path


def discover_files(arguments: list[str]) -> list[Path]:
    """Discover conventions recursively; explicit files may have any name.

    Directories discover bench_*.py, *_bench.py, *.bench.py, and Python files
    under a benchmarks/ directory. Hidden and generated directories are skipped.
    """
    found: set[Path] = set()
    for argument in arguments or ["."]:
        expanded = Path(argument).expanduser()
        paths = (
            [expanded]
            if expanded.exists()
            else [Path(p) for p in glob.glob(str(expanded), recursive=True)]
        )
        if not paths:
            raise ValueError(f"Benchmark path or glob matched nothing: {argument}")
        for path in paths:
            if path.is_file():
                if path.suffix != ".py":
                    raise ValueError(f"Expected a Python benchmark file: {path}")
                found.add(path.resolve())
            elif path.is_dir():
                for root, directories, files in os.walk(path):
                    directories[:] = sorted(
                        d for d in directories if not d.startswith(".") and d not in _EXCLUDED
                    )
                    directory = Path(root)
                    for name in sorted(files):
                        if (
                            name == "__init__.py"
                            or name.startswith(".")
                            or not name.endswith(".py")
                        ):
                            continue
                        if (
                            "benchmarks" in directory.parts
                            or name.startswith("bench_")
                            or name.endswith(("_bench.py", ".bench.py"))
                        ):
                            found.add((directory / name).resolve())
    if not found:
        raise ValueError(
            "No benchmark files found. Use bench_*.py, *_bench.py, *.bench.py, a benchmarks/ directory, or an explicit Python file."
        )
    return sorted(found)


def _load(path: Path) -> ModuleType:
    # A definition may already have been imported by another benchmark file.
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename and Path(filename).resolve() == path:
            return module

    root = path.parent
    parts = [path.stem]
    while (root / "__init__.py").is_file():
        parts.insert(0, root.name)
        root = root.parent
    old_path = sys.path[:]
    sys.path[:0] = [str(root), str(path.parent), str(Path.cwd())]
    try:
        if len(parts) > 1 and all(part.isidentifier() for part in parts):
            name = ".".join(parts)
            module = importlib.import_module(name)
            if Path(module.__file__ or "").resolve() != path:
                raise ImportError(f"Module name {name} resolves to another file: {module.__file__}")
            return module
        name = "_examen_benchmark_" + hashlib.sha256(str(path).encode()).hexdigest()
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        return module
    finally:
        sys.path[:] = old_path


def load_benches(paths: list[Path]) -> list[DiscoveredBench]:
    benches: list[DiscoveredBench] = []
    seen: set[int] = set()
    for path in paths:
        try:
            module = _load(path)
        except Exception as exc:
            raise ImportError(f"Cannot import benchmark {path}: {exc}") from exc
        for variable, value in vars(module).items():
            if isinstance(value, AsyncBench) and id(value) not in seen:
                seen.add(id(value))
                benches.append(DiscoveredBench(path, variable, value))
    if not benches:
        raise ValueError("No module-level AsyncBench objects found in the selected files")
    return benches
