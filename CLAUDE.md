# Examen Python SDK — agent briefing

## What this repo is

The Python SDK for Examen, a generic benchmarking platform. Users install this library, define benchmark cases as Python classes, run them, and the SDK records results either locally (SQLite) or to a remote Go backend (`../examen-backend`).

The SDK's job is to:

1. Let users author benchmarks ergonomically (case classes + scorers).
2. Execute runs (load cases, call `f`, apply scorers, record metrics + trace).
3. Persist results (local SQLite or remote HTTP, same data model).
4. Render reports (Jupyter helpers in `lib/report.py`).

This is **not** an AI-specific library. AI evals are the first concrete domain; the abstractions are deliberately generic so they extend to performance benchmarks, load tests, A/B analyses, etc.

## Key design decisions (already made — do not relitigate without asking)

### Hierarchy

```
User → Project → Bench → (Experiment, Version) → Run → Metric
```

- **Bench** = named, time-bounded grouping of runs across many experiments. (Considered "Session" / "Campaign" / "Study" — settled on Bench.)
- **Experiment** = function `f(case)` + its scorers. Project-scoped, reused across benches.
- **Case** = input to `f`. 1:N with JSON files (one case class, many JSON instances). Immutable by convention.
- **Version** = arbitrary `dict[str, str]` like `{"sdk": "1.2.3", "model": "A"}`. No fixed dimensions globally; SDK enforces schema via pydantic per experiment, server is permissive. Deduped by hash on the server.
- **Run** = one invocation of `f(one_case)` at one version, within a bench. NOT the whole suite execution.
- **Metric** = numeric value + `kind` enum (`pct | duration | currency | ratio | count | raw`). Optional `context` JSON for per-metric rationale (judge output, conversation excerpt, …).

### Test types

Two shapes only:

1. **Binary** (pass/fail) — equality, schema match, contains, ordering, absence.
2. **Scalar metric** — distance, judge score, cost, latency. Threshold optional.

LLM-as-judge is **not** a third type — it's a *source* of values that feeds either shape. Use [`autoevals`](https://github.com/braintrustdata/autoevals) for off-the-shelf rubrics where they fit; write custom judges for domain-specific scoring.

### Metrics are numeric-only

All metric values are numbers. Display formatting is driven by the `kind` enum. Non-numeric context lives in `metrics.context` (per-metric) or `runs.trace` (per-run), never as a metric value itself.

### Trace-then-score, not score-inline

The bench function emits a **structured trace** (steps, intermediate state, raw output) attached to the run. Scorers run **after** against the stored trace. This enables retroactive re-scoring across historical runs — adding a scorer next month does not require re-running cases. Do not write scorers as `assert`-style inline calls inside `f`.

### Setup / teardown

Use DI generators (FastAPI-style `Depends(...)` with `yield`) for fixtures. pytest-style is not the model — scorers do not assert inline.

### Failure ≠ low score

Run status is `succeeded | failed | errored`. A run that crashes has no metric rows, not zero-valued ones. UIs and stats need to distinguish.

### Repeat runs are first-class

Multiple runs with the same `(version, case)` are expected. Used for non-determinism (LLM noise) and statistical comparison. The data model imposes no uniqueness constraint there.

### No case versioning

Cases are immutable by convention. To "change" a case, create a new one with a new name. The unique constraint is `(experiment_id, case_name)`.

## Proposed layout (not yet built)

```
examen/
  lib/                          # reusable framework
    base.py                     # BenchmarkCase, RunResult, Metric, ...
    checks.py                   # Binary, Scalar primitives, registry
    judges.py                   # LLM-as-judge wrappers (autoevals integration)
    runner.py                   # load → run → score → persist
    report.py                   # Jupyter helpers: load_run, render_*, compare_*
    backends/
      local.py                  # SQLite
      remote.py                 # HTTP to examen-backend
  cases/                        # concrete BenchmarkCase subclasses (user "tests")
```

## Open questions / TBD

- Wire format between SDK and Go backend (REST/JSON expected; schema not yet finalized).
- Scorer registration model: code-defined for now; possibly server-registered later for SaaS.
- Trace size limits (small jsonb today; blob store eventually for large traces).
- User/auth model on the SaaS path.

## Conventions for AI assistants working here

- **Pydantic for all user-facing schemas.** Free-form `dict` only at the JSON storage boundary.
- **Don't add features speculatively.** The data model is small on purpose. Confirm with the user before extending.
- **Don't write CLAUDE.md or README.md** unless asked.
- **Don't commit changes unless asked.**

## Related repos

- `../examen-backend` — Go API server.
- `../examen-db` — Postgres migrations (golang-migrate).
