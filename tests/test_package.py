"""Installed-package and legacy entry-point contracts."""

import importlib.metadata
import pathlib
import subprocess
import sys

import pytest

import coval_tracing
import coval_tunnel
import coval_voice
from coval_voice import agent
from coval_voice import client
from coval_voice import config
from coval_voice import errors
from coval_voice import tracing


def test_public_facade_and_legacy_imports_share_definitions():
    expected = {
        "CovalConnectionError": errors.CovalConnectionError,
        "CovalHttpError": errors.CovalHttpError,
        "CovalTraceError": tracing.CovalTraceError,
        "CovalTraceExporter": tracing.CovalTraceExporter,
        "CovalVoiceClient": client.CovalVoiceClient,
        "RunConfig": config.RunConfig,
        "StreamingVoiceAgent": agent.StreamingVoiceAgent,
        "TunnelError": errors.TunnelError,
        "VoiceAgent": agent.VoiceAgent,
    }
    assert set(coval_voice.__all__) == set(expected)
    for name, definition in expected.items():
        assert getattr(coval_voice, name) is definition
    assert coval_tunnel.CovalVoiceClient is coval_voice.CovalVoiceClient
    assert coval_tunnel.RunConfig is coval_voice.RunConfig
    assert coval_tunnel.VoiceAgent is coval_voice.VoiceAgent
    assert coval_tunnel.StreamingVoiceAgent is coval_voice.StreamingVoiceAgent
    assert coval_tunnel.TunnelError is coval_voice.TunnelError
    assert coval_tunnel.CovalTraceExporter is coval_voice.CovalTraceExporter
    assert coval_tunnel.CovalTraceError is coval_voice.CovalTraceError
    assert coval_tracing.CovalTraceExporter is coval_voice.CovalTraceExporter
    assert coval_tracing.CovalTraceError is coval_voice.CovalTraceError


def test_distribution_includes_types_and_no_tests_or_backend():
    package = importlib.metadata.distribution("coval-agent-sdk")
    assert package.metadata["Name"] == "coval-agent-sdk"
    assert pathlib.Path(coval_voice.__file__).with_name("py.typed").is_file()
    files = {str(path) for path in package.files or ()}
    assert not any(path.startswith(("tests/", "backend/")) for path in files)


@pytest.mark.parametrize("module", ["coval_tunnel", "coval_voice.client"])
def test_legacy_module_cli_help_works_without_credentials(module, tmp_path):
    result = subprocess.run(  # noqa: S603 - Fixed interpreter and module list.
        [sys.executable, "-I", "-m", module, "--help"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--agent-module" in result.stdout
    assert "--workspace-id" in result.stdout
    assert "connect" in result.stdout
    assert "run" in result.stdout


@pytest.mark.parametrize(
    "url", ["file:///tmp/api", "ftp://example.test/api", ""]
)
def test_client_rejects_non_http_api_roots(url):
    with pytest.raises(ValueError, match="http or https"):
        coval_voice.CovalVoiceClient(api_key="synthetic-key", api_base_url=url)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8081/v1", "https://api.coval.dev/v1"]
)
def test_client_accepts_http_and_https_api_roots(url):
    voice = coval_voice.CovalVoiceClient(
        api_key="synthetic-key", api_base_url=url
    )
    assert voice.api.api_base_url == url
