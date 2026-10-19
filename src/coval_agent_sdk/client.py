"""Public client facade for launching or attaching voice agents."""

from __future__ import annotations

from coval_agent_sdk import _defaults
from coval_agent_sdk import _http
from coval_agent_sdk import _runs
from coval_agent_sdk import _session
from coval_agent_sdk import agent
from coval_agent_sdk import config
from coval_agent_sdk import results
from coval_agent_sdk import telemetry


class CovalVoiceClient:
    """Launch or attach voice sessions using a per-conversation factory.

    Each conversation owns a fresh agent. Connections are outbound; no listening
    port or agent framework is required. This client does not replay consumed
    relay offers or retry run creation.

    Attributes:
        trace_exporter: Optional process-wide exporter for conversation traces.
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
            api_key: Organization API key; never include it in source or
                diagnostics.
            api_base_url: Coval API root, including the API version path.
            workspace_id: Workspace selector sent on every request, or None to
                use the server's default workspace.
            trace_exporter: Exporter shared with the agent's tracing setup, or
                None.

        Raises:
            ValueError: The API root does not use HTTP or HTTPS.
        """
        self._api = _http.CovalApi(
            api_base_url=api_base_url,
            api_key=api_key,
            workspace_id=workspace_id,
        )
        self.trace_exporter = trace_exporter

    async def connect(
        self, simulation_id: str, agent_factory: agent.AgentFactory
    ) -> results.SessionResult:
        """Attach a fresh agent to an existing simulation.

        Args:
            simulation_id: Identifier of the simulation to attach to.
            agent_factory: Callable receiving the simulation ID and returning a
                new callback or streaming agent. The SDK always closes a created
                agent.

        Returns:
            Completed voice-session identity, audio counts, and trace outcome.

        Raises:
            errors.TunnelError: Discovery, protocol, readiness, or completion
                fails.
            Exception: An agent callback or the relay transport fails.
                Cancellation propagates after local resources are released.
        """
        return await _session.run_session(
            self._api, simulation_id, agent_factory, self.trace_exporter
        )

    async def run(
        self, config: config.RunConfig, agent_factory: agent.AgentFactory
    ) -> results.RunResult:
        """Launch one run and attach a fresh agent to each conversation.

        Args:
            config: Fixture identifiers, concurrency, and total batch deadline.
            agent_factory: Callable receiving a simulation ID and returning a
                fresh agent. It may be called concurrently for different
                conversations.

        Returns:
            Hosted run identity, completion status, and local session outcomes.

        Raises:
            errors.RunError: A launched run fails; result retains its identity,
                last observed hosted status, and completed local sessions.
            errors.TunnelError: Launch, attachment, or monitoring fails, the run
                does not complete successfully, or the batch deadline expires.
                Active local sessions are cancelled; this does not cancel the
                hosted run.
            Exception: Agent or transport setup fails.
        """
        return await _runs.launch_and_run(
            self._api, config, agent_factory, self.trace_exporter
        )
