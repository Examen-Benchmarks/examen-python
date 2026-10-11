# Examen Python SDK

Python SDK for Examen — a generic benchmarking platform.

Examen lets you define benchmarks once and run them across versions of a system, tracking numeric metrics over time. It is domain-agnostic (LLM evals, performance benchmarks, A/B tests, …) and is designed around a small, stable data model.

## Status

Pre-alpha. Not on PyPI yet.

## Concepts

- **Project** — top-level container, one product/system being benchmarked.
- **Bench** — named, time-bounded grouping of runs (e.g., "Tuesday eval"). One bench can span many experiments.
- **Experiment** — a function `f(case)` plus the scorers that grade its output. Project-scoped, reused across benches.
- **Case** — an input passed to `f`. Same case is reused across versions; immutable by convention. 1:N with JSON input files.
- **Version** — a dict of components describing what is being benchmarked, e.g. `{"sdk": "1.2.3", "model": "modelA"}`. Free-form on the server; pydantic-enforced in the SDK per experiment.
- **Run** — one invocation of `f(one_case)` at one version, within a bench.
- **Metric** — a numeric value emitted by a scorer, tagged with a kind (`pct | duration | currency | ratio | count | raw`).

## Quickstart

Requires Python 3.12 or newer. From this checkout:

```bash
uv sync
uv run examen --help
```

In another project's virtual environment, install the SDK from your local checkout:

```bash
uv pip install -e /path/to/examen-python
```

Create `benchmarks/addition.py`:

```python
from pydantic import BaseModel
from examen import AsyncBench, Case, ExactMatchScorer, Ref

class Number(BaseModel):
    value: int

bench = AsyncBench(
    project=Ref(key="calculator", name="Calculator"),
    bench=Ref(key="regression", name="Regression"),
)

@bench.experiment[Number, Number](
    key="add-one",
    name="Add one",
    cases=[
        Case[Number, Number](
            key="one",
            name="1 + 1",
            input=Number(value=1),
            output=Number(value=2),
        )
    ],
    scorers=[ExactMatchScorer[Number, Number]()],
)
async def add_one(input: Number) -> Number:
    return Number(value=input.value + 1)
```

The CLI discovers the module-level `bench`. Do not call `bench.run()` in that
file. Preview the experiments, then run them locally:

```bash
uv run examen run benchmarks/ --list
uv run examen run benchmarks/ --version '{"implementation":"v1"}'
```

No Examen account or server is needed for a local run. Each case result is saved
as plain JSON under `.examen/results/`; the command prints its results directory.

### Run and upload in one command

```bash
export EXAMEN_URL=https://your-examen-server.example
export EXAMEN_API_KEY=your-api-key
uv run examen run benchmarks/ --upload
```

Each result is saved locally **before** upload starts. Upload failures are
retried, and the JSON files remain available after success or failure.
Without `--upload`, execution only saves results locally.

### Send saved results later

Use the results directory printed by the run command:

```bash
uv run examen upload .examen/results/<execution-id>
```

You can also send a specific saved run:

```bash
uv run examen upload .examen/results/<execution-id>/<bench-execution-id>/runs/<run-id>.json
```

Upload requires the same URL and API key configuration shown above. Repeat
`upload` after an outage: acknowledged results are skipped, and benchmarks are
not executed again. Keep the original filenames and IDs.

See the [CLI guide](docs/cli.md) for selection, concurrency, inspection, exit
codes, and migration from `backends=`. See [durable uploads](docs/durable-uploads.md)
for retry settings and backend compatibility.

## Architecture

- **SDK:** define and execute benchmarks; save durable JSON results.
- **CLI:** discover definitions, display progress, and orchestrate uploads.
- **Uploader:** deliver saved results with retries, independently of execution.

These ship in one Python package. `AsyncBench(project=..., bench=...)` does not
accept `backends=`; delivery belongs to the CLI or the separate `Uploader`.

## Related repos

- `examen-backend` — Go HTTP API server.
- `examen-db` — Postgres schema migrations (golang-migrate).
