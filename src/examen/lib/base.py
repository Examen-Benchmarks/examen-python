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


class Metric(BaseModel):
    """A numeric measurement emitted by a scorer for a single run."""

    name: str = Field(description="Scorer-defined identifier, unique within an experiment.")
    kind: MetricKind = Field(description="How the value should be interpreted/displayed.")
    value: float = Field(description="The numeric measurement.")
    context: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional per-metric rationale (judge output, conversation excerpt, …). "
            "Free-form JSON, stored verbatim by the backend."
        ),
    )


class Case(BaseModel, Generic[InputT, OutputT]):
    """Immutable input fixture for an experiment.

    Cases are reused across versions and benches. Their identity within an
    experiment is the (experiment, name) pair; reusing a name with a different
    payload is a server-side conflict (HTTP 409).
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Unique identifier within the experiment.")
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
