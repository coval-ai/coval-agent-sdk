"""Public clients for runs and conversation audio or text connections."""

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
from coval_agent_sdk import text


class _Client:
    """Hold API configuration shared by the voice and text clients."""

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
            api_base_url: HTTPS Coval API root including the version path.
                HTTP is allowed only for localhost or loopback IP addresses.
            workspace_id: Workspace selector, or None for the server default.
            trace_exporter: Exporter shared with application tracing, or None.

        Raises:
            ValueError: The API root does not match a supported URL format.
        """
        self._api = _http.CovalApi(
            api_base_url=api_base_url,
            api_key=api_key,
            workspace_id=workspace_id,
        )
        self.trace_exporter = trace_exporter


class CovalVoiceClient(_Client):
    """Connect existing voice pipelines to Coval through outbound audio.

    The application owns its agent and pipeline lifecycle. This client does not
    construct agents, replay consumed relay offers, or retry run creation.

    Attributes:
        trace_exporter: Optional exporter shared with application tracing.
    """

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
            self._api, audio.AudioConnection(simulation_id), self.trace_exporter
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
        SDK session failures allow unrelated conversations to finish before
        RunError reports their individual outcomes. Authentication failures,
        application errors and global run failures stop local work promptly.

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
            self._api,
            config,
            audio.AudioConnection,
            on_connection,
            self.trace_exporter,
        )

    async def attach(
        self,
        run_id: str,
        on_connection: Callable[[audio.AudioConnection], Awaitable[None]],
        *,
        run_timeout_seconds: float = _defaults.RUN_WAIT_SECONDS,
    ) -> results.RunResult:
        """Serve every conversation of a run your application launched.

        Use this when you create the run through the Coval API yourself, for
        example to select metrics, test cases or metadata. Monitoring, session
        isolation and failure reporting match run(); no run is created.

        Args:
            run_id: Identifier of an existing run whose agent uses the SDK.
            on_connection: Async function accepting one AudioConnection.
            run_timeout_seconds: Positive finite deadline for the whole run,
                including queued conversations and evaluation.

        Returns:
            Hosted run identity, completion status, and conversation outcomes.

        Raises:
            ValueError: run_id is empty or the deadline is invalid.
            errors.RunError: The run fails, retaining partial outcomes.
            Exception: Application errors and cancellation propagate unchanged.
        """
        return await _runs.attach_run(
            self._api,
            run_id,
            run_timeout_seconds,
            audio.AudioConnection,
            on_connection,
            self.trace_exporter,
        )


class CovalTextClient(_Client):
    """Connect existing text agents to Coval's simulated users.

    Each simulated-user message is one turn; reply with one complete message.
    The application owns its agent and conversation state. This client does not
    construct agents, replay consumed relay offers, or retry run creation. Use
    an agent whose type is MODEL_TYPE_SDK_CHAT.

    Attributes:
        trace_exporter: Optional exporter shared with application tracing.
    """

    @contextlib.asynccontextmanager
    async def connect(
        self, simulation_id: str
    ) -> AsyncGenerator[text.TextConnection, None]:
        """Open an existing simulation's text connection after readiness.

        Keep the context open until the conversation ends. Stop application
        tasks and end spans before leaving; the SDK then closes its transport
        and finalizes connection.result. Early normal exit is an SDK failure.

        Args:
            simulation_id: Existing hosted conversation identifier.

        Yields:
            Ready text connection, independent of any agent framework.

        Raises:
            errors.CovalError: Discovery, relay protocol, or early exit fails,
                including a simulation whose agent is not a text SDK agent.
            Exception: Application exceptions and cancellation propagate after
                transport and trace cleanup.
        """
        async with _session.open_session(
            self._api, text.TextConnection(simulation_id), self.trace_exporter
        ) as connection:
            yield connection

    async def run(
        self,
        config: config.RunConfig,
        on_connection: Callable[[text.TextConnection], Awaitable[None]],
    ) -> results.RunResult:
        """Launch a run and deliver each ready text connection to the handler.

        The handler reads simulated-user messages, sends replies, and returns
        after the conversation ends. It may run concurrently for different
        conversations. Failure and cancellation behavior matches
        CovalVoiceClient.run().

        Args:
            config: Fixture identifiers, concurrency, and total run deadline.
            on_connection: Async function accepting one TextConnection.

        Returns:
            Hosted run identity, completion status, and conversation outcomes.

        Raises:
            errors.RunError: A launched run fails, retaining partial outcomes.
            errors.CovalError: Launch or transport fails. Local cleanup does
                not cancel hosted work.
            Exception: Application errors and cancellation propagate unchanged.
        """
        return await _runs.launch_and_run(
            self._api,
            config,
            text.TextConnection,
            on_connection,
            self.trace_exporter,
        )

    async def attach(
        self,
        run_id: str,
        on_connection: Callable[[text.TextConnection], Awaitable[None]],
        *,
        run_timeout_seconds: float = _defaults.RUN_WAIT_SECONDS,
    ) -> results.RunResult:
        """Serve every conversation of a run your application launched.

        Use this when you create the run through the Coval API yourself, for
        example to select metrics, test cases or metadata. Monitoring, session
        isolation and failure reporting match run(); no run is created.

        Args:
            run_id: Identifier of an existing run whose agent uses the SDK.
            on_connection: Async function accepting one TextConnection.
            run_timeout_seconds: Positive finite deadline for the whole run,
                including queued conversations and evaluation.

        Returns:
            Hosted run identity, completion status, and conversation outcomes.

        Raises:
            ValueError: run_id is empty or the deadline is invalid.
            errors.RunError: The run fails, retaining partial outcomes.
            Exception: Application errors and cancellation propagate unchanged.
        """
        return await _runs.attach_run(
            self._api,
            run_id,
            run_timeout_seconds,
            text.TextConnection,
            on_connection,
            self.trace_exporter,
        )
