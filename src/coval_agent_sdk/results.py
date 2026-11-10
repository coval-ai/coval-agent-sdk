"""Immutable outcomes returned by the voice and text simulation clients."""

from __future__ import annotations

import dataclasses
from typing import Literal


@dataclasses.dataclass(frozen=True)
class TraceResult:
    """Describe optional telemetry delivery independently of completion.

    Attributes:
        uploaded_spans: Number of spans delivered by a successful upload, or
            None when a failure leaves the delivered count unknown.
        error: Upload or flush failure, or None on success. Retained exporter
            buffers may be retried without repeating the conversation.
    """

    uploaded_spans: int | None = None
    error: str | None = None


@dataclasses.dataclass(frozen=True)
class ConversationResult:
    """Describe one completed local session after resource cleanup.

    Completion describes transport, not hosted evaluation or quality. Voice
    sessions count audio bytes; text sessions count messages and report zero
    audio bytes.

    Attributes:
        simulation_id: Identifier of the hosted conversation.
        received_audio_bytes: PCM bytes received from the simulated caller.
        sent_audio_bytes: PCM bytes sent by the local agent.
        trace: Telemetry delivery outcome, or None when tracing is disabled.
        received_messages: Text messages received from the simulated user.
        sent_messages: Text replies sent by the local agent.
        status: Local session outcome. Failures raise instead of returning a
            result.
    """

    simulation_id: str
    received_audio_bytes: int
    sent_audio_bytes: int
    trace: TraceResult | None = None
    received_messages: int = 0
    sent_messages: int = 0
    status: Literal["COMPLETED"] = dataclasses.field(
        default="COMPLETED", init=False
    )


@dataclasses.dataclass(frozen=True)
class ConversationFailure:
    """Describe one failed local session after its resource cleanup.

    Attributes:
        simulation_id: Identifier of the affected hosted conversation.
        error_type: Name of the SDK exception type.
        message: SDK diagnostic describing the session failure.
    """

    simulation_id: str
    error_type: str
    message: str


@dataclasses.dataclass(frozen=True)
class RunResult:
    """Describe a hosted run and locally completed conversations.

    A successful return requires COMPLETED. A RunError carries a snapshot whose
    status is the last observed hosted status, not a local cancellation claim.

    Attributes:
        run_id: Identifier returned when the run was created.
        status: Last observed monitoring status, or None before the first
            status response. This does not indicate whether quality criteria
            passed.
        conversations: Completed local sessions in discovery order. Unfinished,
            failed, or missed conversations have no success result.
        failures: SDK session failures in discovery order. Unfinished or
            cancelled sessions and global monitoring failures are excluded.
    """

    run_id: str
    status: str | None
    conversations: tuple[ConversationResult, ...]
    failures: tuple[ConversationFailure, ...] = ()
