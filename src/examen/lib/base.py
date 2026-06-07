"""Core data model: cases, metrics, and run status.

These are the leaf types referenced everywhere else in the SDK. Kept
deliberately small and pydantic-validated so they can be safely serialized to
and from the backend's free-form JSON fields.
"""

from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel | None)


class MetricKind(StrEnum):
    """How a metric value should be interpreted and displayed."""

    PCT = "pct"
    DURATION = "duration"
    CURRENCY = "currency"
    RATIO = "ratio"
    COUNT = "count"
    RAW = "raw"


class RunStatus(StrEnum):
    """Terminal status of a run.

    `succeeded` means the experiment function returned without raising.
    `errored` means the function (or one of its scorers) raised.
    """

    SUCCEEDED = "succeeded"
    ERRORED = "errored"


class Ref(BaseModel):
    """A reference to a named entity: stable `key` + display `name` + optional `description`.

    Per design-decision D15, every named entity (project, bench, collection,
    experiment, case, metric) carries a client-supplied **`key`** — the stable,
    rename-safe business key that find-or-create and ingest resolve by — plus a
    free-form display **`name`** and an optional **`description`**. On ingest the
    `name`/`description` are applied on first create and ignored on reuse
    (first-write-wins); only the `key` participates in identity.

    Used for the project, bench, and each `collection_path` segment in the ingest
    payload. Experiments and cases carry the same trio but add their own fields.
    """

    key: str = Field(description="Stable, rename-safe business key. Identity for find-or-create.")
    name: str = Field(description="Free-form display label.")
    description: str | None = Field(
        default=None, description="Optional longer description, applied on first create."
    )


class Metric(BaseModel):
    """A numeric measurement emitted by a scorer for a single run."""

    key: str = Field(
        description=(
            "Scorer-defined identity, unique per run. The implicit scorer identity is "
            "(experiment, metric key) — the key, not the name."
        )
    )
    name: str = Field(description="Display label for the metric.")
    kind: MetricKind = Field(description="How the value should be interpreted/displayed.")
    value: float = Field(description="The numeric measurement.")
    context: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional per-metric rationale (judge output, conversation excerpt, …). "
            "Free-form JSON, stored verbatim by the backend."
        ),
    )
    description: str | None = Field(
        default=None, description="Optional longer description, applied on first create."
    )


class Case(BaseModel, Generic[InputT, OutputT]):
    """Immutable input fixture for an experiment.

    Cases are reused across versions and benches. Their identity within an
    experiment is the (experiment, key) pair. Cases are **first-write-wins by
    key**: re-running with a different payload (or name/description) under the
    same case key reuses the existing case — the new values are ignored, not
    rejected with a 409. Keeping payloads consistent for a given case key is the
    client's responsibility.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(
        description="Stable identity within the experiment; (experiment, key) is unique."
    )
    name: str = Field(description="Display label for the case.")
    description: str | None = Field(
        default=None, description="Optional longer description, applied on first create."
    )
    input: InputT = Field(description="The input passed to the experiment function.")
    output: OutputT = Field(
        description=(
            "Labeled output. Semantics are scorer-defined: an exact-match scorer "
            "treats it as expected; a load-test scorer may ignore it. Parameterize "
            "as `Case[Input, Output | None]` (or `Case[Input, None]`) when cases "
            "have no labels."
        ),
    )
    repeats: int = Field(
        default=1,
        ge=1,
        description=(
            "How many independent runs to produce per (version, case). Each repeat is "
            "a separate run server-side."
        ),
    )
