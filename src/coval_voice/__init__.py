"""Connect Python voice agents to Coval over outbound WebSockets."""

from coval_voice.agent import StreamingVoiceAgent
from coval_voice.agent import VoiceAgent
from coval_voice.client import CovalVoiceClient
from coval_voice.config import RunConfig
from coval_voice.errors import CovalConnectionError
from coval_voice.errors import CovalHttpError
from coval_voice.errors import TunnelError
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
