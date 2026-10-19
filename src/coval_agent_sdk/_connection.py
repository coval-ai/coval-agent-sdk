"""Discover and claim one-use outbound relay connections."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from urllib import parse

from websockets import exceptions
from websockets.asyncio import client

from coval_agent_sdk import _defaults
from coval_agent_sdk import _http
from coval_agent_sdk import errors


@dataclasses.dataclass(frozen=True)
class ConnectionOffer:
    """Hold one short-lived, attempt-scoped relay credential.

    Attributes:
        wss_url: Secure relay WebSocket URL.
        token: One-use credential, sent only in the upgrade header.
        expires_at: Expiry as Unix epoch seconds.
    """

    wss_url: str
    token: str
    expires_at: float


async def wait_for_offer(
    api: _http.CovalApi, simulation_id: str
) -> ConnectionOffer:
    """Wait until the worker publishes a reachable relay and one-use token."""
    deadline = time.monotonic() + _defaults.CONNECT_WAIT_SECONDS
    path = f"simulations/{parse.quote(simulation_id, safe='')}/connect"
    while time.monotonic() < deadline:
        try:
            connection = await api.get(path)
        except (
            errors.CovalHttpError,
            errors.CovalConnectionError,
        ) as exception:
            if (
                isinstance(exception, errors.CovalHttpError)
                and exception.status_code
                not in _defaults.RETRYABLE_HTTP_STATUSES
            ):
                raise
            await asyncio.sleep(_defaults.POLL_INTERVAL_SECONDS)
            continue
        status = connection.get("status")
        if status == "READY":
            url = connection.get("wss_url")
            token = connection.get("token")
            expires_at = connection.get("expires_at")
            if not isinstance(url, str) or not url.startswith("wss://"):
                raise errors.TunnelError(
                    f"Simulation {simulation_id} returned "
                    f"READY without a WSS URL."
                )
            if not isinstance(token, str) or not token:
                raise errors.TunnelError(
                    f"Simulation {simulation_id} returned "
                    f"READY without a token."
                )
            if (
                not isinstance(expires_at, (int, float))
                or expires_at <= time.time()
            ):
                raise errors.TunnelError(
                    f"Simulation {simulation_id} returned an "
                    f"expired connection offer."
                )
            return ConnectionOffer(
                wss_url=url, token=token, expires_at=float(expires_at)
            )
        if status in _defaults.TERMINAL_CONNECTION_STATUSES:
            raise errors.TunnelError(
                f"Simulation {simulation_id} ended before connecting: {status}."
            )
        # An unattached attempt can expire while the worker prepares its
        # replacement.
        if status not in {"PENDING", "PROVISIONING", "EXPIRED"}:
            raise errors.TunnelError(
                f"Simulation {simulation_id} returned "
                f"unknown connection status {status!r}."
            )
        await asyncio.sleep(_defaults.POLL_INTERVAL_SECONDS)
    raise errors.TunnelError(
        f"Simulation {simulation_id} did not "
        f"publish a relay connection within "
        f"{_defaults.CONNECT_WAIT_SECONDS} "
        f"seconds."
    )


async def open_voice_connection(
    api: _http.CovalApi, simulation_id: str
) -> client.ClientConnection:
    """Retry a live offer through transient WebSocket handshake failures."""
    deadline = time.monotonic() + _defaults.CONNECT_WAIT_SECONDS
    retry_delay_seconds = _defaults.POLL_INTERVAL_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        remaining_seconds = deadline - time.monotonic()
        try:
            offer = await asyncio.wait_for(
                wait_for_offer(api, simulation_id), timeout=remaining_seconds
            )
        except asyncio.TimeoutError as exception:
            raise errors.TunnelError(
                f"Simulation {simulation_id} did not "
                f"offer a usable relay before its "
                f"connect deadline."
            ) from exception
        while time.time() < offer.expires_at and time.monotonic() < deadline:
            try:
                return await client.connect(
                    offer.wss_url,
                    additional_headers={
                        "Authorization": f"Bearer {offer.token}"
                    },
                    open_timeout=max(
                        1.0,
                        min(
                            _defaults.HTTP_TIMEOUT_SECONDS,
                            offer.expires_at - time.time(),
                        ),
                    ),
                    ping_interval=_defaults.WEBSOCKET_PING_INTERVAL_SECONDS,
                    ping_timeout=_defaults.WEBSOCKET_PING_TIMEOUT_SECONDS,
                    max_size=_defaults.VOICE_SAMPLE_RATE_HZ
                    * _defaults.VOICE_SAMPLE_WIDTH_BYTES
                    * 10,
                )
            except (
                OSError,
                TimeoutError,
                exceptions.WebSocketException,
            ) as exception:
                last_error = exception
                remaining_seconds = min(
                    offer.expires_at - time.time(), deadline - time.monotonic()
                )
                if remaining_seconds <= 0:
                    break
                await asyncio.sleep(min(retry_delay_seconds, remaining_seconds))
                retry_delay_seconds = min(
                    retry_delay_seconds * 2, _defaults.CONNECT_RETRY_MAX_SECONDS
                )
        retry_delay_seconds = _defaults.POLL_INTERVAL_SECONDS
    raise errors.TunnelError(
        f"Simulation {simulation_id} could not "
        f"connect to its voice relay "
        f"before the deadline."
    ) from last_error
