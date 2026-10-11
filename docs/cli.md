# Running benchmarks with the Examen CLI

Install this checkout with `uv pip install -e .`. It includes the `examen`
command; `python -m examen`
provides the same interface. In this repository, use `uv run examen`.

## Define benchmarks in Python

Create `benchmarks/addition.py`. Define module-level `AsyncBench` objects; the
CLI finds them after importing the file. Do not call `bench.run()` at import
time. Cases, scorers, collections, and `Depends` work as before.

```python
from pydantic import BaseModel
from examen import AsyncBench, Case, ExactMatchScorer, Ref

class Input(BaseModel):
    a: int
    b: int

class Output(BaseModel):
    result: int

bench = AsyncBench(
    project=Ref(key="calculator", name="Calculator"),
    bench=Ref(key="regression", name="Regression"),
)

@bench.experiment[Input, Output](
    key="addition",
    name="Addition",
    cases=[Case[Input, Output](
        key="one-plus-two", name="1 + 2",
        input=Input(a=1, b=2), output=Output(result=3), repeats=3,
    )],
    scorers=[ExactMatchScorer[Input, Output]()],
)
async def addition(input: Input) -> Output:
    return Output(result=input.a + input.b)
```

## Discover, select, and execute

```bash
examen run
examen run benchmarks/
examen run benchmarks/addition.py
examen run 'benchmarks/**/*.py'
examen run benchmarks/ --list
examen run benchmarks/ --filter regression
examen run benchmarks/ --version '{"model":"candidate","prompt":"v2"}' --concurrency 10
examen run benchmarks/ --output-dir ./results
examen run benchmarks/ --upload --url https://examen.example
```

- With no paths, discovery starts in the current directory. Directory discovery
  includes `bench_*.py`, `*_bench.py`, `*.bench.py`, and Python files anywhere
  under a directory named `benchmarks`. Explicit files can have any name.
- Hidden directories, virtual environments, `node_modules`, build outputs, and
  `__pycache__` are excluded from directory discovery. Explicit file paths can
  select files in those directories.
- Multiple paths and quoted globs are accepted. Files and references to the same
  bench object are deduplicated. Regular Python package imports are supported;
  use `__init__.py` for benchmark packages that need relative imports.
- `--list` imports definitions but does not execute experiments, resolve
  dependencies, or write result artifacts. Import-time Python code still runs.
- `--filter` is a regular expression matched against
  `project/bench/collection/.../experiment` keys or an experiment's display name.
  It works with both `--list` and execution. An empty selection is an error.
- `--version` is a JSON object recorded as version metadata (default `{}`). It
  does not configure your experiment function or choose a model on its behalf.
- `--concurrency` limits simultaneous case invocations, including repeats.
  Benches execute sequentially; cases within each bench run concurrently.
- No Examen URL or API key is required to execute. Your experiment/scorer may
  still need credentials for the services it calls.

## Run and upload in one command

```bash
export EXAMEN_URL=https://your-examen-server.example
export EXAMEN_API_KEY=your-api-key
examen run benchmarks/ --upload
```

Use `--url` to override `EXAMEN_URL`. Without `--upload`, results are saved
locally only, even when credentials are configured.

With `--upload`, each result is saved first and then delivered with retries.
Upload retries can delay execution and progress updates. If delivery exhausts
its retries, the result remains pending locally and execution continues. The
command exits with status 1 if any uploads remain pending. After fixing the
connection or credentials, use `examen upload` on the printed results directory;
you do not need to rerun the benchmark.

## Find your saved results

The CLI prints progress after each result reaches disk, the result directory,
and a final summary. It writes one execution directory per command, containing
one subdirectory per bench invocation:

```text
.examen/results/<execution-id>/
  <bench-execution-id>/
    manifest.json
    runs/
      <run-id>.json
      <run-id>.json
```

Each result is plain JSON, written immediately and atomically. The manifest
records the version, counts, and execution status. On Ctrl+C, completed runs
remain saved. After a hard process kill, the manifest may still say `running`;
completed run files remain independently uploadable. Execution resumption of
unfinished cases is not implemented by `upload`.

## Upload saved results

```bash
export EXAMEN_URL=https://examen.example
export EXAMEN_API_KEY=...
examen upload .examen/results/<execution-id>
```

Use `--url` to override `EXAMEN_URL`. You can also target a bench directory, its
`manifest.json`, a `runs/` directory, or a single run JSON file:

```bash
examen upload .examen/results/<execution-id>/<bench-execution-id>/runs/<run-id>.json
examen upload .examen/results/<execution-id> --max-attempts 20 --retry-delay 2
```

Replace the angle-bracket placeholders with the actual IDs from your results
directory. Retry options apply to the `upload` command; `run --upload` uses the
default retry policy. Keep saved run filenames and IDs unchanged.

Upload does not import benchmark code or invoke experiments/scorers. Repeating
the command skips acknowledged files and retries pending ones. It does not
resume unfinished experiments. See [durable uploads](durable-uploads.md) for
retry and server compatibility details.

## Inspect a saved execution

Print the execution manifest without contacting the backend:

```bash
examen inspect .examen/results/<execution-id>/<bench-execution-id>
```

You can also pass the manifest file directly. Inspection takes a single bench
execution directory, rather than the outer directory containing all benches.
It shows execution counts and status; delivery state is stored in each run's
JSON record.

## Exit codes and help

Exit codes: `0` for successful execution/upload; `1` when experiments errored or
uploads remain pending; `2` for invalid configuration or an operational error;
`130` for Ctrl+C. A low numeric score alone does not change the exit code.

```bash
examen run --help
examen upload --help
examen inspect --help
```

## Using the execution engine directly

For notebooks or an embedded workflow, the SDK remains callable:

```python
result = await bench.run(
    version={"model": "candidate"},
    output_dir="./results",
    concurrency=10,
)
print(result.path, result.completed, result.errored)
```

`bench.list_experiments()` provides metadata without executing. Calling
`bench.run()` saves results locally and returns their directory in `result.path`.
For direct Python delivery, use the separate
[Uploader API](durable-uploads.md#programmatic-uploader).

## Migrating from backend sinks

`AsyncBench` accepts only `project` and `bench`. The `backends=` argument and
the `Backend` / `LocalReportBackend` types have been removed. Remove backend
configuration from benchmark definitions and use `examen run --upload` or
`examen upload <path>` for delivery.

For an offline HTML report, render the saved payloads explicitly:

```python
import json
from pathlib import Path
from examen.lib.report import render_html

payloads = [
    json.loads(path.read_text())["payload"]
    for path in (result.path / "runs").glob("*.json")
]
Path("report.html").write_text(render_html(payloads), encoding="utf-8")
```

## Package boundary

- `examen` exports benchmark authoring and execution types.
- `examen.artifacts` defines the plain JSON artifact models and persistence.
- `examen.upload.Uploader` handles delivery of saved artifacts.
- `examen.cli` orchestrates these public APIs and owns discovery, argument
  parsing, progress display, and exit codes.

The CLI is shipped in the same distribution today, without coupling argument
parsing to the SDK. The previous `Connector` benchmark backend has been replaced
by `Uploader`; remove it from benchmark definitions and run `examen upload`.
