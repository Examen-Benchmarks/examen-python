"""Terminal orchestration. Uses the public SDK and uploader APIs only."""

import argparse
import asyncio
import json
import math
import os
import re
import sys
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from examen import RunProgress
from examen.cli.discovery import DiscoveredBench, benchmark_imports, discover_files, load_benches
from examen.upload import Uploader


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _delay(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return parsed


def _version(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
        json.dumps(parsed, allow_nan=False)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid version JSON: {exc}") from exc
    return parsed


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="examen", description="Run Python benchmarks locally and upload saved JSON results."
    )
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Discover and run benchmarks")
    run.add_argument(
        "paths",
        nargs="*",
        help="Python files, directories, or quoted globs (default: current directory)",
    )
    run.add_argument(
        "--filter",
        dest="filter_pattern",
        help="Regex matching project/bench/experiment key paths or experiment names",
    )
    run.add_argument(
        "--list", action="store_true", help="List matching experiments without running them"
    )
    run.add_argument(
        "--version",
        type=_version,
        default={},
        metavar="JSON",
        help="Version components as a JSON object (default: {})",
    )
    run.add_argument(
        "--concurrency",
        type=_positive,
        default=15,
        help="Maximum concurrent case invocations (default: 15)",
    )
    run.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".examen/results"),
        help="Parent directory for a new execution (default: .examen/results)",
    )

    upload = commands.add_parser(
        "upload", help="Upload saved JSON results without running benchmarks"
    )
    upload.add_argument(
        "path", type=Path, help="Execution directory, runs directory, or JSON run file"
    )
    upload.add_argument(
        "--url", default=os.environ.get("EXAMEN_URL"), help="Examen backend URL (or EXAMEN_URL)"
    )
    upload.add_argument(
        "--max-attempts", type=_positive, default=10, help="Attempts per pending run (default: 10)"
    )
    upload.add_argument(
        "--retry-delay",
        type=_delay,
        default=1.0,
        help="Initial retry delay in seconds (default: 1)",
    )
    return root


def _progress(
    event: RunProgress, *, completed: int, total: int, prefix: str, succeeded: int, errored: int
) -> None:
    print(
        f"[{completed + event.completed}/{total}] {prefix}/{event.experiment} / {event.case} "
        f"({succeeded + event.succeeded} succeeded, {errored + event.errored} errored)",
        flush=True,
    )


async def _run(
    arguments: argparse.Namespace, definitions: list[DiscoveredBench], directory: Path
) -> int:
    completed = succeeded = errored = 0
    total = sum(
        info.runs
        for d in definitions
        for info in d.bench.list_experiments(filter_pattern=arguments.filter_pattern)
    )
    directory.mkdir(parents=True, exist_ok=False)
    print(f"Results: {directory}", flush=True)
    print(f"Running {total} case invocations across {len(definitions)} benchmark(s)", flush=True)
    for definition in definitions:
        prefix = f"{definition.bench.project.key}/{definition.bench.bench.key}"

        result = await definition.bench.run(
            version=arguments.version,
            concurrency=arguments.concurrency,
            output_dir=directory,
            filter_pattern=arguments.filter_pattern,
            on_progress=partial(
                _progress,
                completed=completed,
                total=total,
                prefix=prefix,
                succeeded=succeeded,
                errored=errored,
            ),
        )
        completed += result.completed
        succeeded += result.succeeded
        errored += result.errored
    print(f"Saved {completed} results: {succeeded} succeeded, {errored} errored.", flush=True)
    print(f"Upload later: examen upload {str(directory)!r} --url <backend-url>", flush=True)
    return 1 if errored else 0


async def _upload(arguments: argparse.Namespace, api_key: str) -> int:
    result = await Uploader(
        arguments.url,
        api_key,
        max_attempts=arguments.max_attempts,
        retry_delay=arguments.retry_delay,
    ).upload(arguments.path)
    print(
        f"Uploaded {result.uploaded} run(s); {result.pending} pending. Local JSON files retained."
    )
    for path, error in result.errors.items():
        print(f"{path}: {error}", file=sys.stderr)
    return 1 if result.pending else 0


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    arguments = cli.parse_args(argv)
    directory: Path | None = None
    try:
        if arguments.command == "upload":
            if not arguments.url:
                cli.error("upload requires --url or EXAMEN_URL")
            api_key = os.environ.get("EXAMEN_API_KEY")
            if not api_key:
                cli.error("upload requires EXAMEN_API_KEY in the environment")
            return asyncio.run(_upload(arguments, api_key))
        if arguments.filter_pattern is not None:
            re.compile(arguments.filter_pattern)
        paths = discover_files(arguments.paths)
        with benchmark_imports(paths):
            definitions = load_benches(paths)
            definitions = [
                d
                for d in definitions
                if d.bench.list_experiments(filter_pattern=arguments.filter_pattern)
            ]
            if not definitions:
                raise ValueError("No experiments match the selection")
            if arguments.list:
                for definition in definitions:
                    bench = definition.bench
                    for info in bench.list_experiments(filter_pattern=arguments.filter_pattern):
                        print(
                            f"{bench.project.key}/{bench.bench.key}/{info.path}\t{info.runs} runs\t{definition.source}:{definition.variable}"
                        )
                return 0
            directory = arguments.output_dir.resolve() / str(uuid4())
            return asyncio.run(_run(arguments, definitions, directory))
    except KeyboardInterrupt:
        print(
            f"Interrupted. Completed results remain in {directory}"
            if directory
            else "Interrupted.",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        print(f"examen: {exc}", file=sys.stderr)
        if directory is not None and directory.exists():
            print(f"Completed results remain in {directory}", file=sys.stderr)
        return 2
