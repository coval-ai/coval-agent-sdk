"""Immutable outcomes returned by the voice simulation client."""

from __future__ import annotations

import dataclasses
from typing import Literal


@dataclasses.dataclass(frozen=True)
class TraceResult:
    """Describe optional telemetry delivery independently of voice completion.

    Attributes:
        uploaded_spans: Number of spans delivered to Coval.
        error: Upload or flush failure, or None on success. Retained exporter
            buffers may be retried without repeating the conversation.
    """

    uploaded_spans: int = 0
    error: str | None = None


@dataclasses.dataclass(frozen=True)
class SessionResult:
    """Describe one completed local voice session after resource cleanup.

    Completion describes voice transport, not hosted evaluation or quality.

    Attributes:
        simulation_id: Identifier of the hosted conversation.
        received_audio_bytes: PCM bytes received from the simulated caller.
        sent_audio_bytes: PCM bytes sent by the local agent.
        trace: Telemetry delivery outcome, or None when tracing is disabled.
        status: Local voice-session outcome. Failures raise instead of
            returning a result.
    """

    simulation_id: str
    received_audio_bytes: int
    sent_audio_bytes: int
    trace: TraceResult | None = None
    status: Literal["COMPLETED"] = dataclasses.field(
        default="COMPLETED", init=False
    )


@dataclasses.dataclass(frozen=True)
class RunResult:
    """Describe a hosted run and locally completed voice conversations.

    A successful return requires COMPLETED. A RunError carries a snapshot whose
    status is the last observed hosted status, not a local cancellation claim.

    Attributes:
        run_id: Identifier returned when the run was created.
        status: Last observed monitoring status, or None before the first
            status response. This does not indicate whether quality criteria
            passed.
        conversations: Completed local sessions in discovery order. On failure,
            unfinished, failed, or missed conversations have no success result.
    """

    run_id: str
    status: str | None
    conversations: tuple[SessionResult, ...]
