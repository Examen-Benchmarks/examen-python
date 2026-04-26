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

## Architecture

```
examen/
  lib/                # framework: base classes, checks, runner, report helpers
  cases/              # concrete BenchmarkCase subclasses (the "test code")
# JSON inputs live outside the package — referenced by path/glob from case classes.
```

The SDK can record runs to:

- a **local file backend** (SQLite) — for one-off or offline use,
- the **remote backend** (Go API at `examen-backend`) — for shared / SaaS deployments.

Both speak the same data model.

## Quickstart

TBD — SDK is not yet implemented.

## Related repos

- `examen-backend` — Go HTTP API server.
- `examen-db` — Postgres schema migrations (golang-migrate).
