"""Structured execution traces.

A `Trace` is the structured record of one execution of an experiment function:
timestamps, the input, intermediate steps the function chose to record, and the
final output or error. The runner injects a `Trace` into the function
FastAPI-style and ships it to the backend as part of the run payload.

Scorers receive `Trace` objects (live during the run, or loaded from storage
later) to produce metrics — including retroactively against historical runs
without re-invoking the function.
"""

from datetime import UTC, datetime
from typing import Any, Generic

from pydantic import BaseModel, Field

from examen.lib.base import InputT, OutputT


class TraceStep(BaseModel):
    """One annotated step recorded inside an experiment function."""

    name: str = Field(description="Short label for the step (e.g. 'retrieve', 'llm_call').")
    at: datetime = Field(description="When the step was recorded (UTC).")
    fields: dict[str, Any] = Field(
        description="Arbitrary structured data attached to the step. JSON-serializable."
    )


class Trace(BaseModel, Generic[InputT, OutputT]):
    """Structured record of one execution of an experiment function."""

    case_name: str = Field(description="Name of the case being executed.")
    input: InputT = Field(description="The input passed to the function.")
    started_at: datetime | None = Field(
        default=None, description="When the function was invoked (UTC)."
    )
    finished_at: datetime | None = Field(
        default=None, description="When the function returned or raised (UTC)."
    )
    output: OutputT | None = Field(
        default=None, description="The function's return value, if it succeeded."
    )
    error_message: str | None = Field(
        default=None, description="Exception type and message if the run errored."
    )
    steps: list[TraceStep] = Field(
        default_factory=list, description="User-recorded intermediate steps, in order."
    )

    def step(self, name: str, **fields: Any) -> None:
        """Append a step to the trace.

        Args:
            name: Short label for what this step represents.
            **fields: Arbitrary JSON-serializable data attached to the step.
        """
        self.steps.append(TraceStep(name=name, at=datetime.now(UTC), fields=fields))
