"""Run a local voice adapter over an authenticated outbound Coval relay."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import sys

from coval_voice import _defaults
from coval_voice import _http
from coval_voice import _runs
from coval_voice import _session
from coval_voice import agent
from coval_voice import config as run_config
from coval_voice import errors


def load_agent(module_name: str) -> agent.VoiceAgent:
    """Construct a new agent instance for one conversation."""
    module = importlib.import_module(module_name)
    factory = getattr(module, "create_agent", None)
    if not callable(factory):
        raise errors.TunnelError(
            f"Agent module {module_name!r} must define create_agent()."
        )
    adapter = factory()
    if not isinstance(adapter, agent.VoiceAgent):
        raise errors.TunnelError(
            "create_agent() must return an agent with "
            "on_ready(), on_audio(), and close() "
            "methods."
        )
    return adapter


def parse_arguments() -> argparse.Namespace:
    """Parse CLI options for connecting or launching a run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("COVAL_API_BASE_URL", _defaults.DEFAULT_API_BASE_URL),
    )
    parser.add_argument(
        "--workspace-id", default=os.getenv("COVAL_WORKSPACE_ID")
    )
    parser.add_argument("--agent-module", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    connect_command = commands.add_parser(
        "connect", help="Connect one existing simulation."
    )
    connect_command.add_argument("simulation_id")
    run_command = commands.add_parser(
        "run", help="Launch a run and connect each conversation."
    )
    run_command.add_argument("--agent-id", required=True)
    run_command.add_argument("--persona-id", required=True)
    run_command.add_argument("--test-set-id", required=True)
    run_command.add_argument("--iterations", type=int, default=1)
    run_command.add_argument("--concurrency", type=int, default=1)
    run_command.add_argument(
        "--run-timeout-seconds",
        type=float,
        default=_defaults.RUN_WAIT_SECONDS,
        help=(
            "Total batch deadline including queued calls "
            "(default: 3600 seconds). Expiry cancels "
            "active sessions."
        ),
    )
    run_command.add_argument(
        "--dev-id",
        default=os.getenv("COVAL_DEV_ID"),
        help=(
            "Route this run to a local developer worker "
            "lane (defaults to COVAL_DEV_ID)."
        ),
    )
    return parser.parse_args()


async def main() -> None:
    """Run the requested SDK command."""
    arguments = parse_arguments()
    api_key = os.getenv("COVAL_API_KEY")
    if not api_key:
        raise errors.TunnelError("Set COVAL_API_KEY before connecting.")
    api = _http.CovalApi(
        api_base_url=arguments.api_base_url,
        api_key=api_key,
        workspace_id=arguments.workspace_id,
    )

    def agent_factory(_simulation_id: str) -> agent.VoiceAgent:
        return load_agent(arguments.agent_module)

    if arguments.command == "connect":
        await _session.run_session(api, arguments.simulation_id, agent_factory)
    else:
        config = run_config.RunConfig(
            agent_id=arguments.agent_id,
            persona_id=arguments.persona_id,
            test_set_id=arguments.test_set_id,
            iterations=arguments.iterations,
            concurrency=arguments.concurrency,
            dev_id=arguments.dev_id,
            run_timeout_seconds=arguments.run_timeout_seconds,
        )
        await _runs.launch_and_run(api, config, agent_factory)


def cli() -> None:
    """Run the asynchronous CLI from an installed console script."""
    try:
        asyncio.run(main())
    except (errors.TunnelError, KeyboardInterrupt) as exception:
        print(f"Coval Agent SDK stopped: {exception}", file=sys.stderr)
        raise SystemExit(1) from exception
