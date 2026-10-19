"""Installed public API, license, type marker, and package entry point."""

import importlib.metadata
import pathlib
import subprocess
import sys

import pytest

import coval_voice
from coval_voice import agent
from coval_voice import client
from coval_voice import config
from coval_voice import errors
from coval_voice import tracing


def test_public_facade_exports_the_documented_definitions():
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


def test_distribution_includes_types_and_no_tests_or_backend():
    package = importlib.metadata.distribution("coval-agent-sdk")
    assert package.metadata["Name"] == "coval-agent-sdk"
    assert pathlib.Path(coval_voice.__file__).with_name("py.typed").is_file()
    files = {str(path) for path in package.files or ()}
    assert not any(path.startswith(("tests/", "backend/")) for path in files)


def test_package_cli_help_works_without_credentials(tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", "-m", "coval_voice", "--help"],
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


def test_distribution_declares_and_contains_license_notices():
    package = importlib.metadata.distribution("coval-agent-sdk")
    assert package.metadata["License-Expression"] == "Apache-2.0"
    assert set(package.metadata.get_all("License-File") or ()) == {
        "LICENSE",
        "NOTICE",
    }
    license_files = {
        path.name: package.locate_file(path)
        for path in package.files or ()
        if ".dist-info/licenses/" in str(path)
    }
    assert set(license_files) == {"LICENSE", "NOTICE"}
    license_text = license_files["LICENSE"].read_text()
    assert "Apache License" in license_text
    assert "Version 2.0, January 2004" in license_text
    assert "Copyright 2026 Coval, Inc." in license_files["NOTICE"].read_text()
