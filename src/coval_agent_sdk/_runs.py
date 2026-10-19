"""Launch once, discover conversations, and monitor bounded run completion."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import Any
from urllib import error
from urllib import parse

from coval_agent_sdk import _defaults
from coval_agent_sdk import _http
from coval_agent_sdk import _session
from coval_agent_sdk import agent
from coval_agent_sdk import config as run_config
from coval_agent_sdk import errors
from coval_agent_sdk import results
from coval_agent_sdk import telemetry


async def _get_monitoring_resource(
    api: _http.CovalApi,
    path: str,
    *,
    run_deadline: float,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Retry a monitoring read within the original run deadline."""
    retry_deadline = min(
        run_deadline, time.monotonic() + _defaults.MONITORING_RETRY_SECONDS
    )
    last_error: Exception | None = None
    while time.monotonic() < retry_deadline:
        remaining_seconds = retry_deadline - time.monotonic()
        try:
            return await asyncio.wait_for(
                api.get(path, query=query), timeout=remaining_seconds
            )
        except errors.CovalHttpError as exception:
            if exception.status_code not in _defaults.RETRYABLE_HTTP_STATUSES:
                raise
            last_error = exception
        except (errors.CovalConnectionError, error.URLError) as exception:
            last_error = exception
        except asyncio.TimeoutError as exception:
            raise errors.TunnelError(
                f"Coval monitoring GET {path} exceeded its retry deadline."
            ) from exception
        await asyncio.sleep(
            max(
                0.0,
                min(
                    _defaults.POLL_INTERVAL_SECONDS,
                    retry_deadline - time.monotonic(),
                ),
            )
        )
    raise errors.TunnelError(
        f"Coval monitoring GET {path} exceeded its retry deadline."
    ) from last_error


@dataclasses.dataclass
class _RunState:
    """Retain the hosted identity and last observed status through failure."""

    run_id: str | None = None
    status: str | None = None

    def observe_status(self, status: object) -> None:
        """Validate and retain the status supplied by the hosted API.

        Args:
            status: Untrusted status field from a monitoring response.

        Raises:
            errors.TunnelError: The response has no string status.
        """
        if not isinstance(status, str):
            raise errors.TunnelError("Run monitoring returned no status.")
        self.status = status


async def launch_and_run(
    api: _http.CovalApi,
    config: run_config.RunConfig,
    agent_factory: agent.AgentFactory,
    trace_exporter: telemetry.TraceExporter | None = None,
) -> results.RunResult:
    """Launch once and retain completed outcomes through run failures."""
    active: dict[str, asyncio.Task[results.SessionResult]] = {}
    state = _RunState()
    try:
        try:
            return await _launch_and_run(
                api, config, agent_factory, trace_exporter, active, state
            )
        finally:
            unfinished = [task for task in active.values() if not task.done()]
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)
    except errors.TunnelError as exception:
        if state.run_id is None:
            raise
        raise errors.RunError(
            str(exception),
            result=results.RunResult(
                run_id=state.run_id,
                status=state.status,
                conversations=_completed_sessions(active),
            ),
        ) from exception


def _completed_sessions(
    active: dict[str, asyncio.Task[results.SessionResult]],
) -> tuple[results.SessionResult, ...]:
    """Collect successful local outcomes without consuming failed tasks."""
    return tuple(
        task.result()
        for task in active.values()
        if task.done() and not task.cancelled() and task.exception() is None
    )


async def _launch_and_run(
    api: _http.CovalApi,
    config: run_config.RunConfig,
    agent_factory: agent.AgentFactory,
    trace_exporter: telemetry.TraceExporter | None,
    active: dict[str, asyncio.Task[results.SessionResult]],
    state: _RunState,
) -> results.RunResult:
    """Monitor the launched run while the caller owns task cleanup."""
    launch_payload: dict[str, Any] = {
        "agent_id": config.agent_id,
        "persona_id": config.persona_id,
        "test_set_id": config.test_set_id,
        "options": {
            "iteration_count": config.iterations,
            "concurrency": config.concurrency,
        },
    }
    if config.dev_id:
        launch_payload["dev_id"] = config.dev_id
    launch_response = await api.post("runs", launch_payload)
    launch_run: dict[str, Any] = launch_response.get("run") or {}
    run_id = launch_run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise errors.TunnelError("POST /runs returned no run.run_id.")
    state.run_id = run_id
    seen: set[str] = set()
    deadline = time.monotonic() + config.run_timeout_seconds
    run_status = "PENDING"
    while time.monotonic() < deadline:
        await _attach_simulations(
            api, run_id, deadline, agent_factory, trace_exporter, seen, active
        )
        for simulation_id, task in active.items():
            if not task.done():
                continue
            failure = task.exception()
            if failure is not None:
                raise errors.TunnelError(
                    f"Voice session {simulation_id} failed: {failure}"
                ) from failure
        run_response = await _get_monitoring_resource(
            api, f"runs/{parse.quote(run_id, safe='')}", run_deadline=deadline
        )
        run_resource: dict[str, Any] = run_response.get("run") or {}
        state.observe_status(run_resource.get("status"))
        run_status = state.status
        if run_status in _defaults.TERMINAL_RUN_STATUSES and all(
            task.done() for task in active.values()
        ):
            break
        await asyncio.sleep(_defaults.POLL_INTERVAL_SECONDS)
    if time.monotonic() >= deadline:
        raise errors.TunnelError(
            f"Run {run_id} did not finish within "
            f"{config.run_timeout_seconds:g} "
            f"seconds."
        )
    if not active:
        raise errors.TunnelError(
            f"Run {run_id} finished without a voice session to attach."
        )
    if len(active) != len(seen):
        raise errors.TunnelError(
            f"Run {run_id} had "
            f"{len(seen) - len(active)} "
            f"conversation(s) finish before the SDK "
            f"attached."
        )
    failures: list[str] = []
    for simulation_id, task in active.items():
        try:
            await task
        except Exception as exception:
            failures.append(f"{simulation_id}: {exception}")
    if failures:
        raise errors.TunnelError(
            "Voice sessions failed: " + "; ".join(failures)
        )
    if run_status != "COMPLETED":
        raise errors.TunnelError(
            f"Run {run_id} finished with status {run_status}."
        )

    return results.RunResult(
        run_id=run_id,
        status=run_status,
        conversations=_completed_sessions(active),
    )


async def _attach_simulations(
    api: _http.CovalApi,
    run_id: str,
    deadline: float,
    agent_factory: agent.AgentFactory,
    trace_exporter: telemetry.TraceExporter | None,
    seen: set[str],
    active: dict[str, asyncio.Task[results.SessionResult]],
) -> None:
    """Discover every page and record each fresh task before monitoring resumes.

    The caller owns cancellation of tasks in active, including when listing a
    later page fails. Seen identifiers include conversations that ended before
    attachment, so final validation cannot silently omit them.
    """
    page_token: str | None = None
    while True:
        query = {"filter": f'run_id="{run_id}"', "page_size": "100"}
        if page_token:
            query["page_token"] = page_token
        listing = await _get_monitoring_resource(
            api, "simulations", query=query, run_deadline=deadline
        )
        for simulation in listing.get("simulations", []):
            simulation_id = simulation.get("simulation_id")
            if not isinstance(simulation_id, str) or simulation_id in seen:
                continue
            seen.add(simulation_id)
            if (
                simulation.get("status")
                in _defaults.TERMINAL_SIMULATION_STATUSES
            ):
                continue
            active[simulation_id] = asyncio.create_task(
                _session.run_session(
                    api, simulation_id, agent_factory, trace_exporter
                )
            )
        page_token = listing.get("next_page_token")
        if not page_token:
            break
