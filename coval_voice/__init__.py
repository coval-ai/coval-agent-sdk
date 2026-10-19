"""Connect Python voice agents to Coval over authenticated outbound WebSockets."""

from coval_voice.client import CovalConnectionError
from coval_voice.client import CovalHttpError
from coval_voice.client import CovalVoiceClient
from coval_voice.client import RunConfig
from coval_voice.client import StreamingVoiceAgent
from coval_voice.client import TunnelError
from coval_voice.client import VoiceAgent
from coval_voice.tracing import CovalTraceError
from coval_voice.tracing import CovalTraceExporter

__all__ = [
  "CovalConnectionError",
  "CovalHttpError",
  "CovalTraceError",
  "CovalTraceExporter",
  "CovalVoiceClient",
  "RunConfig",
  "StreamingVoiceAgent",
  "TunnelError",
  "VoiceAgent",
]
