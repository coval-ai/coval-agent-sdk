"""Immutable configuration for launching a voice simulation run."""

from __future__ import annotations

import dataclasses
import math

from coval_voice import _defaults


@dataclasses.dataclass(frozen=True)
class RunConfig:
    """Identify the fixtures, concurrency, and deadline of a voice run.

    Attributes:
        agent_id: Coval agent fixture identifier.
        persona_id: Simulated caller persona identifier.
        test_set_id: Test set containing the conversation scenarios.
        iterations: Positive number of iterations per scenario.
        concurrency: Positive maximum number of concurrent conversations.
        dev_id: Optional local worker lane; omit for hosted workers.
        run_timeout_seconds: Positive finite batch deadline in seconds,
            including queued conversations and evaluation postprocessing.

    Raises:
        ValueError: A required identifier is empty or a numeric limit is
            invalid.
    """

    agent_id: str
    persona_id: str
    test_set_id: str
    iterations: int = 1
    concurrency: int = 1
    dev_id: str | None = None
    run_timeout_seconds: float = _defaults.RUN_WAIT_SECONDS

    def __post_init__(self) -> None:
        """Validate fixture identifiers and numeric limits."""
        if not self.agent_id or not self.persona_id or not self.test_set_id:
            raise ValueError(
                "RunConfig requires agent_id, persona_id, and test_set_id."
            )
        if self.iterations < 1 or self.concurrency < 1:
            raise ValueError(
                "RunConfig iterations and concurrency must be positive."
            )
        if (
            not math.isfinite(self.run_timeout_seconds)
            or self.run_timeout_seconds <= 0
        ):
            raise ValueError(
                "RunConfig run_timeout_seconds must be positive and finite."
            )
