"""Public client for runs and conversation audio connections."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from collections.abc import Awaitable
from collections.abc import Callable
import contextlib

from coval_agent_sdk import _defaults
from coval_agent_sdk import _http
from coval_agent_sdk import _runs
from coval_agent_sdk import _session
from coval_agent_sdk import audio
from coval_agent_sdk import config
from coval_agent_sdk import results
from coval_agent_sdk import telemetry


class CovalVoiceClient:
    """Connect existing voice pipelines to Coval through outbound audio.

    The application owns its agent and pipeline lifecycle. This client does not
    construct agents, replay consumed relay offers, or retry run creation.

    Attributes:
        trace_exporter: Optional exporter shared with application tracing.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_base_url: str = _defaults.DEFAULT_API_BASE_URL,
        workspace_id: str | None = None,
        trace_exporter: telemetry.TraceExporter | None = None,
    ) -> None:
        """Configure authentication, workspace selection, and optional traces.

        Args:
            api_key: Organization API key; never include it in diagnostics.
            api_base_url: Coval API root including the version path.
            workspace_id: Workspace selector, or None for the server default.
            trace_exporter: Exporter shared with application tracing, or None.

        Raises:
            ValueError: The API root does not use HTTP or HTTPS.
        """
        self._api = _http.CovalApi(
            api_base_url=api_base_url,
            api_key=api_key,
            workspace_id=workspace_id,
        )
        self.trace_exporter = trace_exporter

    @contextlib.asynccontextmanager
    async def connect(
        self, simulation_id: str
    ) -> AsyncGenerator[audio.AudioConnection, None]:
        """Open an existing simulation's audio connection after relay readiness.

        Keep the context open until the conversation ends. Stop application
        tasks and end spans before leaving; the SDK then closes its transport
        and finalizes connection.result. Early normal exit is an SDK failure.

        Args:
            simulation_id: Existing hosted conversation identifier.

        Yields:
            Ready audio connection, independent of any agent framework.

        Raises:
            errors.CovalError: Discovery, relay protocol, or early exit fails.
            Exception: Application exceptions and cancellation propagate after
                transport and trace cleanup.
        """
        async with _session.open_session(
            self._api, simulation_id, self.trace_exporter
        ) as connection:
            yield connection

    async def run(
        self,
        config: config.RunConfig,
        on_connection: Callable[[audio.AudioConnection], Awaitable[None]],
    ) -> results.RunResult:
        """Launch a run and deliver each ready connection to the application.

        The handler wires an existing pipeline, stays active until voice
        completion, and releases application resources before returning. It
        may run concurrently for different conversations. A transport failure
        cancels and awaits the handler, allowing its finally blocks to run.

        Args:
            config: Fixture identifiers, concurrency, and total run deadline.
            on_connection: Async function accepting one AudioConnection; it
                returns no agent or result object.

        Returns:
            Hosted run identity, completion status, and conversation outcomes.

        Raises:
            errors.RunError: A launched run fails, retaining partial outcomes.
            errors.CovalError: Launch or transport fails. Local cleanup does
                not cancel hosted work.
            Exception: Application errors and cancellation propagate unchanged.
        """
        return await _runs.launch_and_run(
            self._api, config, on_connection, self.trace_exporter
        )
