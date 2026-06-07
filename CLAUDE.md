# Examen Python SDK — agent briefing

## What this repo is

The Python SDK for Examen, a generic benchmarking platform. Users install this
library, author benchmarks as typed Python (experiment functions + cases +
scorers), run them, and the SDK records results either locally (SQLite/report)
or to the remote Go backend (`../examen-backend`) — same data model both ways.

The SDK's job:

1. Author benchmarks ergonomically (typed experiment functions, cases, scorers).
2. Execute runs (load cases, call `f`, capture a structured trace, score after).
3. Persist results (local or remote HTTP) via the ingest contract.
4. Render reports (Jupyter helpers in `lib/report.py`).

Not AI-specific — AI evals are the first domain; the abstractions are generic so
they extend to performance benchmarks, load tests, A/B analyses, etc.

## The data model is owned by `../examen-docs` — do not duplicate it here

The canonical concept reference and the locked decision log live in
[`../examen-docs`](../examen-docs) (`concepts/*.md` + `design-decisions.md`).
**`examen-docs` wins on any conflict.** Repo `CLAUDE.md` files previously drifted
from the real model and caused rework — so this file points at the source of
truth instead of restating it.

Current hierarchy (see `examen-docs/concepts/overview.md`):

```
Team(*) → Project → Bench → Collection (nestable, bench-scoped) → Experiment → Case
Run → (bench, experiment, case, version) → Metric
```

(*) teams deferred — the SDK sends no `user_id`/team; the server resolves the
caller from `Authorization: Bearer <api_key>` (D9).

Decisions that shape SDK code (full text in `examen-docs/design-decisions.md`):

- **D15 — every named entity has a client-supplied `key` + display `name` +
  optional `description`.** `key` is the stable, rename-safe business key that
  find-or-create / ingest resolve by; `name` is a free-form label. Applies to
  projects, benches, collections, experiments, cases, metrics. Versions and runs
  have no name → no key.
- **D2 — collections** are a bench-scoped nestable tree (the `APIRouter` to the
  bench's `app`): `@bench.experiment` = root-mounted, `@collection.experiment` =
  under a collection.
- **D4 / D5 — no iteration entity.** Repeated runs of the same `(version, case)`
  ARE the iterations (each repeat is its own run row; aggregation is read-time).
  The SDK models this with `Case.repeats`. Note the vocabulary trap: the model's
  "run" is the leaf (one `f(case)`); a "benchmark result for an experiment at a
  version" is a *group* of runs, not a row. Don't add an iteration object.
- **D8 — cases immutable, first-write-wins by `key`.** A later run with a
  different payload under the same case key **reuses** the existing case (payload
  ignored, not a 409).
- **D6 — failure ≠ low score** (failed/errored runs carry an error message and
  zero metric rows). **D10 — metrics numeric only** (non-numeric → `metric.context`
  or `run.trace`; scorer identity is `(experiment, metric key)`).

## SDK-specific design (not in examen-docs — preserve these)

- **Trace-then-score, never inline.** The experiment function emits a structured
  `Trace`; scorers run *after*, against the stored trace, so historical runs can
  be re-scored by adding a scorer later. No `assert`-style scoring inside `f`.
- **DI via FastAPI-style `Depends(...)` generators** (`lib/depends.py`) for
  fixtures/setup-teardown. pytest-style is not the model.
- **Pydantic for every user-facing schema.** Free-form `dict` only at the JSON
  storage boundary (case payload, version components, trace, metric context).
- **Mandatory-subscript experiment decorator** — `@bench.experiment[In, Out](...)`;
  there is intentionally no untyped form (the subscript type-checks
  cases/scorers/summarizers against the function signature).
- **Backends fan out.** A bench holds N backends (`lib/backends/`: `http.py`
  Connector → `POST /ingest/runs`; `local.py` LocalReportBackend → SQLite/HTML);
  each completed run goes to all of them, same payload.
- **Async-only today** (`AsyncBench`/`AsyncScorer`). `SyncBench`/`Bench` are
  planned, not built — don't add them unprompted.

## Status

The SDK is mid-migration from the old name-based model to the keys + collections
model above. The one-time build plan is
[`../examen-docs/SDK-REDESIGN-PLAN.md`](../examen-docs/SDK-REDESIGN-PLAN.md) —
follow it for that work; it is transient and will be removed once done.

## Build / test commands

- `make check` — the canonical gate: `ruff` (format-check + lint + import sort),
  `mypy --strict`, then `pytest` with coverage. Run before declaring done.
- `make lint` — autofix. `make build` — `mypy --strict` + `uv lock`.
  `make test` — pytest + coverage (`htmlcov/`). (`make run` is a stale template
  leftover — ignore it.)

## Conventions for AI assistants working here

- **`examen-docs` is canonical** — read it; don't restate or fork the model here.
- **Pydantic for all user-facing schemas.** Free-form `dict` only at the JSON boundary.
- **Don't add features speculatively.** The model is small on purpose; confirm extensions.
- **Don't write CLAUDE.md or README.md** unless asked. **Don't commit** unless asked.

## Related repos

- `../examen-docs` — canonical concepts + locked decision log (wins on conflict).
- `../examen-backend` — Go API server; owns the OpenAPI spec and ingest endpoint.
- `../examen-db` — Postgres schema (Atlas; `schema.sql` is the readable snapshot).
