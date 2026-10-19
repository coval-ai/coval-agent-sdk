"""Public client facade for launching or attaching voice agents."""

from __future__ import annotations

from coval_voice import _defaults
from coval_voice import _http
from coval_voice import _runs
from coval_voice import _session
from coval_voice import agent
from coval_voice import config
from coval_voice import tracing
from coval_voice._audio import send_paced_audio as send_paced_audio

# Preserve previously importable names without coupling internal layers to this
# facade.
from coval_voice._cli import cli as cli
from coval_voice._cli import load_agent as load_agent
from coval_voice._cli import main as main
from coval_voice._cli import parse_arguments as parse_arguments
from coval_voice._connection import ConnectionOffer as ConnectionOffer
from coval_voice._connection import (
    open_voice_connection as open_voice_connection,
)
from coval_voice._connection import wait_for_offer as wait_for_offer
from coval_voice._http import CovalApi as CovalApi
from coval_voice._runs import launch_and_run as launch_and_run
from coval_voice._session import run_session as run_session
from coval_voice.agent import AgentFactory as AgentFactory
from coval_voice.agent import StreamingVoiceAgent as StreamingVoiceAgent
from coval_voice.agent import VoiceAgent as VoiceAgent
from coval_voice.config import RunConfig as RunConfig
from coval_voice.errors import CovalConnectionError as CovalConnectionError
from coval_voice.errors import CovalHttpError as CovalHttpError
from coval_voice.errors import TunnelError as TunnelError
from coval_voice.tracing import CovalTraceError as CovalTraceError
from coval_voice.tracing import CovalTraceExporter as CovalTraceExporter


class CovalVoiceClient:
    """Launch or attach voice sessions using a per-conversation factory.

    Each conversation owns a fresh agent. Connections are outbound; no listening
    port or agent framework is required. This client does not replay consumed
    relay offers or retry run creation.

    Attributes:
        api: Authenticated HTTP transport used by the client.
        trace_exporter: Optional process-wide exporter for conversation traces.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_base_url: str = _defaults.DEFAULT_API_BASE_URL,
        workspace_id: str | None = None,
        trace_exporter: tracing.CovalTraceExporter | None = None,
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
        self.api = _http.CovalApi(
            api_base_url=api_base_url,
            api_key=api_key,
            workspace_id=workspace_id,
        )
        self.trace_exporter = trace_exporter

    async def connect(
        self, simulation_id: str, agent_factory: agent.AgentFactory
    ) -> None:
        """Attach a fresh agent to an existing simulation.

        Args:
            simulation_id: Identifier of the simulation to attach to.
            agent_factory: Callable receiving the simulation ID and returning a
                new callback or streaming agent. The SDK always closes a created
                agent.

        Raises:
            errors.TunnelError: Discovery, protocol, readiness, or completion
                fails.
            Exception: An agent callback or the relay transport fails.
                Cancellation propagates after local resources are released.
        """
        await _session.run_session(
            self.api, simulation_id, agent_factory, self.trace_exporter
        )

    async def run(
        self, config: config.RunConfig, agent_factory: agent.AgentFactory
    ) -> None:
        """Launch one run and attach a fresh agent to each conversation.

        Args:
            config: Fixture identifiers, concurrency, and total batch deadline.
            agent_factory: Callable receiving a simulation ID and returning a
                fresh agent. It may be called concurrently for different
                conversations.

        Raises:
            errors.TunnelError: Launch, attachment, or monitoring fails, the run
                does not complete successfully, or the batch deadline expires.
                Active local sessions are cancelled; this does not cancel the
                hosted run.
            Exception: Agent or transport setup fails.
        """
        await _runs.launch_and_run(
            self.api, config, agent_factory, self.trace_exporter
        )


if __name__ == "__main__":
    cli()
