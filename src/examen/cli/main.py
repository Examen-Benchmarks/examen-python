"""Terminal orchestration. Uses the public SDK and uploader APIs only."""

import asyncio
import json
import math
import os
import re
import sys
from functools import partial
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import typer

from examen import RunProgress
from examen.cli.discovery import DiscoveredBench, benchmark_imports, discover_files, load_benches
from examen.upload import Uploader


def _exit_with_result(result: int) -> None:
    raise typer.Exit(result)


app = typer.Typer(
    add_completion=False,
    help="Run Python benchmarks and deliver JSON results.",
    result_callback=_exit_with_result,
)


def _delay(value: float) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise typer.BadParameter("must be a finite nonnegative number")
    return parsed


def _version(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
        json.dumps(parsed, allow_nan=False)
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid version JSON: {exc}") from exc
    return parsed


def _progress(
    event: RunProgress, *, completed: int, total: int, prefix: str, succeeded: int, errored: int
) -> None:
    print(
        f"[{completed + event.completed}/{total}] {prefix}/{event.experiment} / {event.case} "
        f"({succeeded + event.succeeded} succeeded, {errored + event.errored} errored)",
        flush=True,
    )


async def _run(
    definitions: list[DiscoveredBench],
    directory: Path,
    *,
    version: dict[str, Any],
    concurrency: int,
    filter_pattern: str | None,
    uploader: Uploader | None,
) -> int:
    completed = succeeded = errored = 0
    total = sum(
        info.runs
        for d in definitions
        for info in d.bench.list_experiments(filter_pattern=filter_pattern)
    )
    directory.mkdir(parents=True, exist_ok=False)
    pending_uploads = 0

    async def upload_saved(path: Path) -> None:
        nonlocal pending_uploads
        if uploader is not None:
            sync = await uploader.upload(path)
            pending_uploads += sync.pending

    print(f"Results: {directory}", flush=True)
    print(f"Running {total} case invocations across {len(definitions)} benchmark(s)", flush=True)
    for definition in definitions:
        prefix = f"{definition.bench.project.key}/{definition.bench.bench.key}"

        result = await definition.bench.run(
            version=version,
            concurrency=concurrency,
            output_dir=directory,
            filter_pattern=filter_pattern,
            on_progress=partial(
                _progress,
                completed=completed,
                total=total,
                prefix=prefix,
                succeeded=succeeded,
                errored=errored,
            ),
            on_run_saved=upload_saved if uploader is not None else None,
        )
        completed += result.completed
        succeeded += result.succeeded
        errored += result.errored
    print(f"Saved {completed} results: {succeeded} succeeded, {errored} errored.", flush=True)
    print(f"Upload later: examen upload {str(directory)!r} --url <backend-url>", flush=True)
    return 1 if errored or pending_uploads else 0


async def _upload(path: Path, url: str, api_key: str, max_attempts: int, retry_delay: float) -> int:
    result = await Uploader(
        url,
        api_key,
        max_attempts=max_attempts,
        retry_delay=retry_delay,
    ).upload(path)
    print(
        f"Uploaded {result.uploaded} run(s); {result.pending} pending. Local JSON files retained."
    )
    for failed_path, error in result.errors.items():
        print(f"{failed_path}: {error}", file=sys.stderr)
    return 1 if result.pending else 0


def _inspect(path: Path) -> int:
    manifest = path / "manifest.json" if path.is_dir() else path
    if not manifest.is_file():
        raise ValueError(f"Manifest not found: {manifest}")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    print(json.dumps(data, indent=2, default=str))
    return 0


@app.command("run")
def run_command(
    paths: Annotated[
        list[str] | None, typer.Argument(help="Python files, directories, or quoted globs")
    ] = None,
    filter_pattern: Annotated[
        str | None, typer.Option("--filter", help="Regex matching experiment paths or names")
    ] = None,
    list_only: Annotated[
        bool, typer.Option("--list", help="List experiments without executing")
    ] = False,
    version: Annotated[
        str, typer.Option(help="Version components as a JSON object", metavar="JSON")
    ] = "{}",
    concurrency: Annotated[
        int, typer.Option(min=1, help="Maximum concurrent case invocations")
    ] = 15,
    output_dir: Annotated[Path, typer.Option(help="Parent directory for a new execution")] = Path(
        ".examen/results"
    ),
    upload: Annotated[
        bool, typer.Option("--upload", help="Upload each saved result as it completes")
    ] = False,
    url: Annotated[str | None, typer.Option(envvar="EXAMEN_URL", help="Backend URL")] = None,
) -> int:
    """Discover and run benchmarks."""
    components = _version(version)
    uploader = None
    if upload and not list_only:
        api_key = os.environ.get("EXAMEN_API_KEY")
        if not url or not api_key:
            raise typer.BadParameter("--upload requires --url (or EXAMEN_URL) and EXAMEN_API_KEY")
        uploader = Uploader(url, api_key)
    directory: Path | None = None
    try:
        if filter_pattern is not None:
            re.compile(filter_pattern)
        discovered = discover_files(paths or [])
        with benchmark_imports(discovered):
            definitions = [
                d
                for d in load_benches(discovered)
                if d.bench.list_experiments(filter_pattern=filter_pattern)
            ]
            if not definitions:
                raise ValueError("No experiments match the selection")
            if list_only:
                for definition in definitions:
                    bench = definition.bench
                    for info in bench.list_experiments(filter_pattern=filter_pattern):
                        print(
                            f"{bench.project.key}/{bench.bench.key}/{info.path}\t{info.runs} runs\t{definition.source}:{definition.variable}"
                        )
                return 0
            directory = output_dir.resolve() / str(uuid4())
            return asyncio.run(
                _run(
                    definitions,
                    directory,
                    version=components,
                    concurrency=concurrency,
                    filter_pattern=filter_pattern,
                    uploader=uploader,
                )
            )
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


@app.command("upload")
def upload_command(
    path: Annotated[
        Path, typer.Argument(help="Execution directory, runs directory, or JSON run file")
    ],
    url: Annotated[str | None, typer.Option(envvar="EXAMEN_URL", help="Examen backend URL")] = None,
    max_attempts: Annotated[int, typer.Option(min=1, help="Attempts per pending run")] = 10,
    retry_delay: Annotated[
        float, typer.Option(callback=_delay, help="Initial retry delay in seconds")
    ] = 1.0,
) -> int:
    """Upload saved JSON results without running benchmarks."""
    if not url:
        raise typer.BadParameter("upload requires --url or EXAMEN_URL")
    api_key = os.environ.get("EXAMEN_API_KEY")
    if not api_key:
        raise typer.BadParameter("upload requires EXAMEN_API_KEY in the environment")
    return asyncio.run(_upload(path, url, api_key, max_attempts, retry_delay))


@app.command("inspect")
def inspect_command(
    path: Annotated[Path, typer.Argument(help="Execution directory or manifest.json")],
) -> int:
    """Inspect a saved execution manifest."""
    return _inspect(path)


def main(argv: list[str] | None = None) -> int:
    """Use Typer for both the installed command and explicit argument lists."""
    try:
        app(args=argv, prog_name="examen")
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except (KeyboardInterrupt, typer.Abort):
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"examen: {exc}", file=sys.stderr)
        return 2
