#!/usr/bin/env python3
"""Run a local agent over a Coval SDK WebSocket voice relay."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
import argparse
import asyncio
from collections.abc import AsyncIterator
from collections.abc import Callable
from dataclasses import dataclass
import importlib
import json
import math
import os
import sys
import time
from typing import Any
from typing import Protocol
from typing import runtime_checkable
from urllib import error
from urllib import parse
from urllib import request

from opentelemetry import trace
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from websockets.exceptions import WebSocketException

from coval_voice.tracing import CovalTraceError
from coval_voice.tracing import CovalTraceExporter

DEFAULT_API_BASE_URL = "https://api.coval.dev/v1"
HTTP_TIMEOUT_SECONDS = 15
POLL_INTERVAL_SECONDS = 2
CONNECT_WAIT_SECONDS = 300
CONNECT_RETRY_MAX_SECONDS = 8
RUN_WAIT_SECONDS = 3_600
MONITORING_RETRY_SECONDS = 30
WEBSOCKET_PING_INTERVAL_SECONDS = 20
WEBSOCKET_PING_TIMEOUT_SECONDS = 20
VOICE_SAMPLE_RATE_HZ = 16_000
VOICE_CHANNEL_COUNT = 1
VOICE_SAMPLE_WIDTH_BYTES = 2
VOICE_CHUNK_DURATION_SECONDS = 0.02
VOICE_CHUNK_BYTES = int(
  VOICE_SAMPLE_RATE_HZ * VOICE_CHANNEL_COUNT * VOICE_SAMPLE_WIDTH_BYTES * VOICE_CHUNK_DURATION_SECONDS
)
MAX_PENDING_REPLY_BYTES = 2 * 1024 * 1024
MAX_PENDING_REPLIES = 4
CLOSE_CONFIRM_TIMEOUT_SECONDS = 10
TERMINAL_CONNECTION_STATUSES = {"CONNECTED", "CLOSED", "FAILED", "CANCELLED"}
TERMINAL_RUN_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}
TERMINAL_SIMULATION_STATUSES = TERMINAL_RUN_STATUSES | {"SKIPPED", "DELETED"}
RETRYABLE_HTTP_STATUSES = {409, 429, 500, 502, 503, 504}


class TunnelError(RuntimeError):
  """The Coval API or relay could not complete a voice session."""


def _add_cleanup_note(exception: BaseException, note: str) -> None:
  """Attach cleanup context when the Python runtime supports exception notes."""
  add_note = getattr(exception, "add_note", None)
  if callable(add_note):
    add_note(note)


class CovalHttpError(TunnelError):
  """An HTTP error returned by the Coval API."""

  def __init__(self, *, status_code: int, message: str) -> None:
    super().__init__(message)
    self.status_code = status_code


class CovalConnectionError(TunnelError):
  """An API request could not reach Coval."""


@runtime_checkable
class VoiceAgent(Protocol):
  """A local agent that consumes and optionally emits PCM16 mono 16 kHz audio."""

  async def on_ready(self) -> bytes | None:
    """Return optional greeting audio after the Coval pipeline is ready."""

  async def on_audio(self, audio: bytes) -> bytes | None:
    """Enqueue audio promptly and return optional reply audio.

    Sustained processing must keep pace with incoming audio, normally one chunk
    every 20 ms. Slow ASR or other remote work belongs in a separate task with a
    bounded queue. This callback blocks reception of the next frame; the relay
    closes the session if unacknowledged audio exceeds 64 KiB (about two seconds).
    """

  async def close(self) -> None:
    """Release resources after the socket closes."""


class StreamingVoiceAgent(ABC):
  """Produce PCM independently of incoming audio using a bounded async stream.

  Subclass this adapter for agents whose TTS runs in background tasks. The SDK
  pulls output only after on_ready completes and paces it in 20 ms chunks. Keep
  your producer queue bounded; an output error fails the session and calls close.
  """

  async def on_ready(self) -> None:
    """Start the agent after the relay is ready, returning promptly."""

  @abstractmethod
  async def on_audio(self, audio: bytes) -> None:
    """Enqueue incoming PCM16 mono 16 kHz audio without slow remote processing."""

  @abstractmethod
  def audio_output(self) -> AsyncIterator[bytes]:
    """Yield PCM16 mono 16 kHz output, including speech during input silence.

    Keep yielding until the session ends. Returning early is a session failure.
    Empty chunks are ignored. Each nonempty chunk must fit within 2 MiB.
    """

  @abstractmethod
  async def close(self) -> None:
    """Cancel and await all agent tasks and finish any conversation spans."""


AgentFactory = Callable[[str], VoiceAgent | StreamingVoiceAgent]
TRACE_FLUSH_TIMEOUT_MILLISECONDS = 5_000


@dataclass(frozen=True)
class RunConfig:
  """Identify the Coval fixtures and local worker lane for a voice run."""

  agent_id: str
  persona_id: str
  test_set_id: str
  iterations: int = 1
  concurrency: int = 1
  dev_id: str | None = None
  run_timeout_seconds: float = RUN_WAIT_SECONDS

  def __post_init__(self) -> None:
    if not self.agent_id or not self.persona_id or not self.test_set_id:
      raise ValueError("RunConfig requires agent_id, persona_id, and test_set_id.")
    if self.iterations < 1 or self.concurrency < 1:
      raise ValueError("RunConfig iterations and concurrency must be positive.")
    if not math.isfinite(self.run_timeout_seconds) or self.run_timeout_seconds <= 0:
      raise ValueError("RunConfig run_timeout_seconds must be positive and finite.")


@dataclass(frozen=True)
class ConnectionOffer:
  """One short-lived, attempt-scoped connection offer."""

  wss_url: str
  token: str
  expires_at: float


class CovalApi:
  """Small HTTP client for the MVP, independent of generated SDK models."""

  def __init__(self, *, api_base_url: str, api_key: str, workspace_id: str | None = None) -> None:
    self.api_base_url = api_base_url.rstrip("/")
    self.api_key = api_key
    self.workspace_id = workspace_id

  async def get(self, path: str, *, query: dict[str, str] | None = None) -> dict[str, Any]:
    """Fetch a JSON resource without blocking the audio event loop."""
    return await asyncio.to_thread(self._request, "GET", path, query, None)

  async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Post a JSON resource without automatically retrying mutations."""
    return await asyncio.to_thread(self._request, "POST", path, None, payload)

  def _request(
    self,
    method: str,
    path: str,
    query: dict[str, str] | None,
    payload: dict[str, Any] | None,
  ) -> dict[str, Any]:
    url = f"{self.api_base_url}/{path.lstrip('/')}"
    if query:
      url = f"{url}?{parse.urlencode(query)}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
      "x-api-key": self.api_key,
      "Accept": "application/json",
      "Content-Type": "application/json",
    }
    if self.workspace_id is not None:
      headers["X-Coval-Workspace-Id"] = self.workspace_id
    api_request = request.Request(
      url,
      data=body,
      method=method,
      headers=headers,
    )
    try:
      with request.urlopen(api_request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        response_payload = json.load(response)
    except error.HTTPError as exception:
      response_body = exception.read(500).decode("utf-8", errors="replace")
      raise CovalHttpError(
        status_code=exception.code,
        message=f"Coval {method} {path} returned HTTP {exception.code}: {response_body}",
      ) from exception
    except error.URLError as exception:
      raise CovalConnectionError(f"Coval {method} {path} could not reach {url}: {exception.reason}") from exception
    if not isinstance(response_payload, dict):
      raise TunnelError(f"Coval {method} {path} returned a non-object JSON response.")
    return response_payload


class CovalVoiceClient:
  """Launch or attach voice sessions using an agent factory."""

  def __init__(
    self,
    *,
    api_key: str,
    api_base_url: str = DEFAULT_API_BASE_URL,
    workspace_id: str | None = None,
    trace_exporter: CovalTraceExporter | None = None,
  ) -> None:
    self.api = CovalApi(api_base_url=api_base_url, api_key=api_key, workspace_id=workspace_id)
    self.trace_exporter = trace_exporter

  async def connect(self, simulation_id: str, agent_factory: AgentFactory) -> None:
    """Attach a fresh agent to an existing simulation."""
    await run_session(self.api, simulation_id, agent_factory, self.trace_exporter)

  async def run(self, config: RunConfig, agent_factory: AgentFactory) -> None:
    """Launch a run and attach a fresh agent to each conversation."""
    await launch_and_run(self.api, config, agent_factory, self.trace_exporter)


async def wait_for_offer(api: CovalApi, simulation_id: str) -> ConnectionOffer:
  """Wait until the worker publishes a reachable relay and one-use token."""
  deadline = time.monotonic() + CONNECT_WAIT_SECONDS
  path = f"simulations/{parse.quote(simulation_id, safe='')}/connect"
  while time.monotonic() < deadline:
    try:
      connection = await api.get(path)
    except (CovalHttpError, CovalConnectionError) as exception:
      if isinstance(exception, CovalHttpError) and exception.status_code not in RETRYABLE_HTTP_STATUSES:
        raise
      await asyncio.sleep(POLL_INTERVAL_SECONDS)
      continue
    status = connection.get("status")
    if status == "READY":
      url = connection.get("wss_url")
      token = connection.get("token")
      expires_at = connection.get("expires_at")
      if not isinstance(url, str) or not url.startswith("wss://"):
        raise TunnelError(f"Simulation {simulation_id} returned READY without a WSS URL.")
      if not isinstance(token, str) or not token:
        raise TunnelError(f"Simulation {simulation_id} returned READY without a token.")
      if not isinstance(expires_at, (int, float)) or expires_at <= time.time():
        raise TunnelError(f"Simulation {simulation_id} returned an expired connection offer.")
      return ConnectionOffer(wss_url=url, token=token, expires_at=float(expires_at))
    if status in TERMINAL_CONNECTION_STATUSES:
      raise TunnelError(f"Simulation {simulation_id} ended before connecting: {status}.")
    # An unattached attempt can expire while the worker prepares its replacement.
    if status not in {"PENDING", "PROVISIONING", "EXPIRED"}:
      raise TunnelError(f"Simulation {simulation_id} returned unknown connection status {status!r}.")
    await asyncio.sleep(POLL_INTERVAL_SECONDS)
  raise TunnelError(
    f"Simulation {simulation_id} did not publish a relay connection within {CONNECT_WAIT_SECONDS} seconds."
  )


async def send_paced_audio(websocket: Any, audio: bytes, *, next_send_at: float | None = None) -> float:
  """Send PCM16 in real-time chunks so the worker is not flooded."""
  if len(audio) % VOICE_SAMPLE_WIDTH_BYTES:
    raise TunnelError(f"Agent returned {len(audio)} bytes of PCM16 audio; an even byte count is required.")
  if next_send_at is None:
    next_send_at = time.monotonic()
  for offset in range(0, len(audio), VOICE_CHUNK_BYTES):
    chunk = audio[offset : offset + VOICE_CHUNK_BYTES]
    await asyncio.sleep(max(0.0, next_send_at - time.monotonic()))
    await websocket.send(chunk)
    next_send_at = max(next_send_at, time.monotonic()) + len(chunk) / (
      VOICE_SAMPLE_RATE_HZ * VOICE_CHANNEL_COUNT * VOICE_SAMPLE_WIDTH_BYTES
    )
  return next_send_at


async def open_voice_connection(api: CovalApi, simulation_id: str) -> ClientConnection:
  """Retry a live offer through transient WebSocket handshake failures."""
  deadline = time.monotonic() + CONNECT_WAIT_SECONDS
  retry_delay_seconds = POLL_INTERVAL_SECONDS
  last_error: Exception | None = None
  while time.monotonic() < deadline:
    remaining_seconds = deadline - time.monotonic()
    try:
      offer = await asyncio.wait_for(wait_for_offer(api, simulation_id), timeout=remaining_seconds)
    except asyncio.TimeoutError as exception:  # noqa: UP041 - asyncio uses a separate exception on Python 3.10.
      raise TunnelError(
        f"Simulation {simulation_id} did not offer a usable relay before its connect deadline."
      ) from exception
    while time.time() < offer.expires_at and time.monotonic() < deadline:
      try:
        return await connect(
          offer.wss_url,
          additional_headers={"Authorization": f"Bearer {offer.token}"},
          open_timeout=max(1.0, min(HTTP_TIMEOUT_SECONDS, offer.expires_at - time.time())),
          ping_interval=WEBSOCKET_PING_INTERVAL_SECONDS,
          ping_timeout=WEBSOCKET_PING_TIMEOUT_SECONDS,
          max_size=VOICE_SAMPLE_RATE_HZ * VOICE_SAMPLE_WIDTH_BYTES * 10,
        )
      except (OSError, TimeoutError, WebSocketException) as exception:
        last_error = exception
        remaining_seconds = min(offer.expires_at - time.time(), deadline - time.monotonic())
        if remaining_seconds <= 0:
          break
        await asyncio.sleep(min(retry_delay_seconds, remaining_seconds))
        retry_delay_seconds = min(retry_delay_seconds * 2, CONNECT_RETRY_MAX_SECONDS)
    retry_delay_seconds = POLL_INTERVAL_SECONDS
  raise TunnelError(
    f"Simulation {simulation_id} could not connect to its voice relay before the deadline."
  ) from last_error


def load_agent(module_name: str) -> VoiceAgent:
  """Construct a new agent instance for one conversation."""
  module = importlib.import_module(module_name)
  factory = getattr(module, "create_agent", None)
  if not callable(factory):
    raise TunnelError(f"Agent module {module_name!r} must define create_agent().")
  agent = factory()
  if not isinstance(agent, VoiceAgent):
    raise TunnelError("create_agent() must return an agent with on_ready(), on_audio(), and close() methods.")
  return agent


async def run_session(
  api: CovalApi,
  simulation_id: str,
  agent_factory: AgentFactory,
  trace_exporter: CovalTraceExporter | None = None,
) -> None:
  """Connect one simulation and exchange binary audio with a fresh agent."""
  agent = agent_factory(simulation_id)
  received_pcm_bytes = 0
  sent_pcm_bytes = 0
  pending_reply_bytes = 0
  replies: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MAX_PENDING_REPLIES)
  sender: asyncio.Task[None] | None = None
  completed = False
  session_started = False
  session_error: BaseException | None = None

  def queue_reply(audio: bytes) -> None:
    nonlocal pending_reply_bytes
    if pending_reply_bytes + len(audio) > MAX_PENDING_REPLY_BYTES or replies.full():
      raise TunnelError(f"Simulation {simulation_id} agent replies exceeded the bounded audio send queue.")
    pending_reply_bytes += len(audio)
    replies.put_nowait(audio)

  async def send_replies(websocket: ClientConnection) -> None:
    nonlocal sent_pcm_bytes, pending_reply_bytes
    while True:
      audio = await replies.get()
      await send_paced_audio(websocket, audio)
      sent_pcm_bytes += len(audio)
      pending_reply_bytes -= len(audio)

  async def send_stream(websocket: ClientConnection) -> None:
    nonlocal sent_pcm_bytes
    if not isinstance(agent, StreamingVoiceAgent):
      raise TypeError("Streaming audio requires a StreamingVoiceAgent.")
    stream = agent.audio_output()
    next_send_at: float | None = None
    try:
      async for audio in stream:
        if not isinstance(audio, bytes) or len(audio) % VOICE_SAMPLE_WIDTH_BYTES:
          raise TunnelError("Agent output must be even-length PCM16 bytes.")
        if len(audio) > MAX_PENDING_REPLY_BYTES:
          raise TunnelError("Agent output chunk exceeds the 2 MiB limit; yield smaller chunks.")
        if audio:
          next_send_at = await send_paced_audio(websocket, audio, next_send_at=next_send_at)
          sent_pcm_bytes += len(audio)
        else:
          await asyncio.sleep(0)
      raise TunnelError(f"Simulation {simulation_id} agent audio stream ended before session_end.")
    finally:
      close_stream = getattr(stream, "aclose", None)
      if close_stream is not None:
        await close_stream()

  async def send_audio(websocket: ClientConnection, ready: asyncio.Event) -> None:
    await ready.wait()
    if isinstance(agent, StreamingVoiceAgent):
      await send_stream(websocket)
    else:
      await send_replies(websocket)

  try:
    connection = await open_voice_connection(api, simulation_id)
    async with connection:
      websocket = connection
      print(f"[{simulation_id}] Connected to Coval voice relay.", flush=True)
      ready = asyncio.Event()
      sender = asyncio.create_task(send_audio(websocket, ready))
      async for message in receive_while_sending(websocket, sender, ready_event=ready):
        if isinstance(message, str):
          control = json.loads(message)
          if not isinstance(control, dict):
            raise TunnelError(f"Simulation {simulation_id} returned invalid control JSON.")
          message_type = control.get("type")
          if message_type == "session_ready":
            audio_format = control.get("audio")
            expected_format = {
              "encoding": "pcm_s16le",
              "sample_rate_hz": VOICE_SAMPLE_RATE_HZ,
              "channels": VOICE_CHANNEL_COUNT,
            }
            if audio_format != expected_format:
              raise TunnelError(
                f"Simulation {simulation_id} requires unsupported audio format "
                f"{audio_format!r}; expected {expected_format!r}."
              )
            greeting = await agent.on_ready()
            session_started = True
            ready.set()
            if greeting:
              queue_reply(greeting)
          elif message_type == "session_end":
            print(f"[{simulation_id}] Coval ended the session.", flush=True)
            completed = True
            return
          elif message_type == "error":
            raise TunnelError(f"Simulation {simulation_id} reported an error: {control.get('message')}")
          continue
        if not ready.is_set():
          raise TunnelError(f"Simulation {simulation_id} sent audio before session_ready.")
        if len(message) % VOICE_SAMPLE_WIDTH_BYTES:
          raise TunnelError(
            f"Simulation {simulation_id} sent {len(message)} PCM16 bytes; an even byte count is required."
          )
        received_pcm_bytes += len(message)
        await websocket.send(json.dumps({"type": "audio_ack", "received_bytes": received_pcm_bytes}))
        reply = await agent.on_audio(message)
        if reply:
          queue_reply(reply)
      if session_started and await _confirm_completed_after_close(api, simulation_id):
        completed = True
        print(f"[{simulation_id}] Coval confirmed completion after the relay closed.", flush=True)
        return
      raise TunnelError(f"Simulation {simulation_id} closed its voice relay before session_end.")
  except ConnectionClosed as exception:
    session_error = exception
    if exception.rcvd is not None and exception.rcvd.code == 1013:
      session_error = TunnelError(
        f"Simulation {simulation_id} exceeded the voice relay's audio backlog limit. "
        "Keep on_audio callbacks fast by enqueueing audio for background processing in a bounded queue."
      )
      raise session_error from exception
    close_code = exception.rcvd.code if exception.rcvd is not None else None
    if (
      session_started and close_code in {None, 1000, 1001} and await _confirm_completed_after_close(api, simulation_id)
    ):
      completed = True
      session_error = None
      print(f"[{simulation_id}] Coval confirmed completion after the relay closed.", flush=True)
      return
    session_error = exception
    raise
  except BaseException as exception:
    session_error = exception
    raise
  finally:
    cleanup_error: BaseException | None = None
    if sender is not None:
      sender.cancel()
      await asyncio.gather(sender, return_exceptions=True)
    try:
      await agent.close()
    except BaseException as exception:
      if session_error is not None:
        _add_cleanup_note(session_error, f"Agent cleanup also failed: {exception!r}")
      else:
        cleanup_error = exception
    if trace_exporter is not None:
      try:
        provider = trace.get_tracer_provider()
        flush = getattr(provider, "force_flush", None)
        if not callable(flush) or not await asyncio.to_thread(flush, timeout_millis=TRACE_FLUSH_TIMEOUT_MILLISECONDS):
          raise CovalTraceError(f"OpenTelemetry could not flush spans for simulation {simulation_id}.")
        if completed:
          span_count = await asyncio.to_thread(
            trace_exporter.upload,
            simulation_id=simulation_id,
            api_base_url=api.api_base_url,
            api_key=api.api_key,
            workspace_id=api.workspace_id,
          )
          print(f"[{simulation_id}] Uploaded {span_count} trace spans.", flush=True)
      except BaseException as exception:
        primary_error = session_error if session_error is not None else cleanup_error
        if primary_error is not None:
          _add_cleanup_note(primary_error, f"Trace cleanup also failed: {exception!r}")
        elif completed and isinstance(exception, CovalTraceError):
          print(
            f"[{simulation_id}] Voice session completed, but trace export failed: {exception}. "
            "Retry trace upload from the retained exporter; do not rerun the conversation.",
            file=sys.stderr,
            flush=True,
          )
        else:
          cleanup_error = exception
      finally:
        if not completed:
          trace_exporter.discard(simulation_id=simulation_id)
    print(
      f"[{simulation_id}] PCM audio: received {received_pcm_bytes} bytes; sent {sent_pcm_bytes} bytes.",
      flush=True,
    )
    if cleanup_error is not None:
      raise cleanup_error


async def _confirm_completed_after_close(api: CovalApi, simulation_id: str) -> bool:
  """Accept a lost terminal control frame only after Coval confirms completion."""
  deadline = time.monotonic() + CLOSE_CONFIRM_TIMEOUT_SECONDS
  while time.monotonic() < deadline:
    remaining_seconds = deadline - time.monotonic()
    try:
      response = await asyncio.wait_for(
        api.get(f"simulations/{parse.quote(simulation_id, safe='')}"), timeout=remaining_seconds
      )
    except (TunnelError, json.JSONDecodeError, asyncio.TimeoutError):  # noqa: UP041
      return False
    simulation = response.get("simulation")
    if not isinstance(simulation, dict):
      return False
    status = simulation.get("status")
    if status == "COMPLETED":
      return True
    if status not in {"PENDING", "QUEUED", "IN PROGRESS"}:
      return False
    await asyncio.sleep(max(0.0, min(POLL_INTERVAL_SECONDS, deadline - time.monotonic())))
  return False


async def receive_while_sending(
  websocket: ClientConnection,
  sender: asyncio.Task[None],
  *,
  ready_event: asyncio.Event | None = None,
) -> AsyncIterator[str | bytes]:
  """Keep receiving audio during paced replies and surface sender failures."""
  messages = aiter(websocket)
  ready_deadline = time.monotonic() + CONNECT_WAIT_SECONDS
  while True:
    receive = asyncio.ensure_future(anext(messages))
    try:
      readiness_pending = ready_event is not None and not ready_event.is_set()
      timeout_seconds = max(0, ready_deadline - time.monotonic()) if readiness_pending else None
      done, _ = await asyncio.wait((receive, sender), timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
      if not done:
        raise TunnelError(f"Voice relay did not send session_ready within {CONNECT_WAIT_SECONDS} seconds.")
      if sender in done:
        await sender
      yield await receive
    except StopAsyncIteration:
      return
    finally:
      if not receive.done():
        receive.cancel()
      await asyncio.gather(receive, return_exceptions=True)


async def _get_monitoring_resource(
  api: CovalApi,
  path: str,
  *,
  run_deadline: float,
  query: dict[str, str] | None = None,
) -> dict[str, Any]:
  """Retry an idempotent monitoring read without extending the run deadline."""
  retry_deadline = min(run_deadline, time.monotonic() + MONITORING_RETRY_SECONDS)
  last_error: Exception | None = None
  while time.monotonic() < retry_deadline:
    remaining_seconds = retry_deadline - time.monotonic()
    try:
      return await asyncio.wait_for(api.get(path, query=query), timeout=remaining_seconds)
    except CovalHttpError as exception:
      if exception.status_code not in RETRYABLE_HTTP_STATUSES:
        raise
      last_error = exception
    except (CovalConnectionError, error.URLError) as exception:
      last_error = exception
    except asyncio.TimeoutError as exception:  # noqa: UP041 - asyncio uses a separate exception on Python 3.10.
      raise TunnelError(f"Coval monitoring GET {path} exceeded its retry deadline.") from exception
    await asyncio.sleep(max(0.0, min(POLL_INTERVAL_SECONDS, retry_deadline - time.monotonic())))
  raise TunnelError(f"Coval monitoring GET {path} exceeded its retry deadline.") from last_error


async def launch_and_run(
  api: CovalApi,
  config: RunConfig,
  agent_factory: AgentFactory,
  trace_exporter: CovalTraceExporter | None = None,
) -> None:
  """Launch a run and attach one local agent per discovered simulation."""
  active: dict[str, asyncio.Task[None]] = {}
  try:
    await _launch_and_run(api, config, agent_factory, trace_exporter, active)
  finally:
    unfinished = [task for task in active.values() if not task.done()]
    for task in unfinished:
      task.cancel()
    if unfinished:
      await asyncio.gather(*unfinished, return_exceptions=True)


async def _launch_and_run(
  api: CovalApi,
  config: RunConfig,
  agent_factory: AgentFactory,
  trace_exporter: CovalTraceExporter | None,
  active: dict[str, asyncio.Task[None]],
) -> None:
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
  run_id = (launch_response.get("run") or {}).get("run_id")
  if not isinstance(run_id, str) or not run_id:
    raise TunnelError("POST /runs returned no run.run_id.")
  print(f"Launched run {run_id}.", flush=True)
  seen: set[str] = set()
  deadline = time.monotonic() + config.run_timeout_seconds
  run_status = "PENDING"
  while time.monotonic() < deadline:
    page_token: str | None = None
    while True:
      query = {"filter": f'run_id="{run_id}"', "page_size": "100"}
      if page_token:
        query["page_token"] = page_token
      listing = await _get_monitoring_resource(api, "simulations", query=query, run_deadline=deadline)
      for simulation in listing.get("simulations", []):
        simulation_id = simulation.get("simulation_id")
        if not isinstance(simulation_id, str) or simulation_id in seen:
          continue
        seen.add(simulation_id)
        if simulation.get("status") in TERMINAL_SIMULATION_STATUSES:
          continue
        active[simulation_id] = asyncio.create_task(run_session(api, simulation_id, agent_factory, trace_exporter))
        print(f"[{simulation_id}] Discovered conversation.", flush=True)
      page_token = listing.get("next_page_token")
      if not page_token:
        break
    for simulation_id, task in active.items():
      if not task.done():
        continue
      failure = task.exception()
      if failure is not None:
        raise TunnelError(f"Voice session {simulation_id} failed: {failure}") from failure
    run_response = await _get_monitoring_resource(api, f"runs/{parse.quote(run_id, safe='')}", run_deadline=deadline)
    run_status = (run_response.get("run") or {}).get("status")
    if run_status in TERMINAL_RUN_STATUSES and all(task.done() for task in active.values()):
      break
    await asyncio.sleep(POLL_INTERVAL_SECONDS)
  if time.monotonic() >= deadline:
    raise TunnelError(f"Run {run_id} did not finish within {config.run_timeout_seconds:g} seconds.")
  if not active:
    raise TunnelError(f"Run {run_id} finished without a voice session to attach.")
  if len(active) != len(seen):
    raise TunnelError(f"Run {run_id} had {len(seen) - len(active)} conversation(s) finish before the SDK attached.")
  failures: list[str] = []
  for simulation_id, task in active.items():
    try:
      await task
    except Exception as exception:
      failures.append(f"{simulation_id}: {exception}")
  if failures:
    raise TunnelError("Voice sessions failed: " + "; ".join(failures))
  print(
    f"Run {run_id} finished with status {run_status} and {len(active)} voice session(s).",
    flush=True,
  )
  if run_status != "COMPLETED":
    raise TunnelError(f"Run {run_id} finished with status {run_status}.")


def parse_arguments() -> argparse.Namespace:
  """Parse CLI options for connecting or launching a run."""
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--api-base-url", default=os.getenv("COVAL_API_BASE_URL", DEFAULT_API_BASE_URL))
  parser.add_argument("--workspace-id", default=os.getenv("COVAL_WORKSPACE_ID"))
  parser.add_argument("--agent-module", required=True)
  commands = parser.add_subparsers(dest="command", required=True)
  connect_command = commands.add_parser("connect", help="Connect one existing simulation.")
  connect_command.add_argument("simulation_id")
  run_command = commands.add_parser("run", help="Launch a run and connect each conversation.")
  run_command.add_argument("--agent-id", required=True)
  run_command.add_argument("--persona-id", required=True)
  run_command.add_argument("--test-set-id", required=True)
  run_command.add_argument("--iterations", type=int, default=1)
  run_command.add_argument("--concurrency", type=int, default=1)
  run_command.add_argument(
    "--run-timeout-seconds",
    type=float,
    default=RUN_WAIT_SECONDS,
    help="Total batch deadline including queued calls (default: 3600 seconds). Expiry cancels active sessions.",
  )
  run_command.add_argument(
    "--dev-id",
    default=os.getenv("COVAL_DEV_ID"),
    help="Route this run to a local developer worker lane (defaults to COVAL_DEV_ID).",
  )
  return parser.parse_args()


async def main() -> None:
  """Run the requested SDK command."""
  arguments = parse_arguments()
  api_key = os.getenv("COVAL_API_KEY")
  if not api_key:
    raise TunnelError("Set COVAL_API_KEY before connecting.")
  api = CovalApi(api_base_url=arguments.api_base_url, api_key=api_key, workspace_id=arguments.workspace_id)

  def agent_factory(_simulation_id: str) -> VoiceAgent:
    return load_agent(arguments.agent_module)

  if arguments.command == "connect":
    await run_session(api, arguments.simulation_id, agent_factory)
  else:
    config = RunConfig(
      agent_id=arguments.agent_id,
      persona_id=arguments.persona_id,
      test_set_id=arguments.test_set_id,
      iterations=arguments.iterations,
      concurrency=arguments.concurrency,
      dev_id=arguments.dev_id,
      run_timeout_seconds=arguments.run_timeout_seconds,
    )
    await launch_and_run(api, config, agent_factory)


def cli() -> None:
  """Run the asynchronous CLI from an installed console script."""
  try:
    asyncio.run(main())
  except (TunnelError, KeyboardInterrupt) as exception:
    print(f"Coval Agent SDK stopped: {exception}", file=sys.stderr)
    raise SystemExit(1) from exception


if __name__ == "__main__":
  cli()
