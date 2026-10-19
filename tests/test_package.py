"""Installed public API, license, type marker, and library-only distribution."""

import importlib.metadata
import pathlib
import subprocess
import sys

from packaging import requirements
import pytest

import coval_agent_sdk
from coval_agent_sdk import agent
from coval_agent_sdk import client
from coval_agent_sdk import config
from coval_agent_sdk import errors
from coval_agent_sdk import results
from coval_agent_sdk import telemetry


def test_public_facade_exports_the_documented_definitions():
    expected = {
        "CovalConnectionError": errors.CovalConnectionError,
        "CovalHttpError": errors.CovalHttpError,
        "CovalTraceError": errors.CovalTraceError,
        "CovalVoiceClient": client.CovalVoiceClient,
        "RunConfig": config.RunConfig,
        "RunError": errors.RunError,
        "RunResult": results.RunResult,
        "SessionResult": results.SessionResult,
        "TraceResult": results.TraceResult,
        "TraceExporter": telemetry.TraceExporter,
        "StreamingVoiceAgent": agent.StreamingVoiceAgent,
        "TunnelError": errors.TunnelError,
        "VoiceAgent": agent.VoiceAgent,
    }
    assert set(coval_agent_sdk.__all__) == set(expected)
    for name, definition in expected.items():
        assert getattr(coval_agent_sdk, name) is definition


def test_distribution_includes_types_and_no_tests_or_backend():
    package = importlib.metadata.distribution("coval-agent-sdk")
    assert package.metadata["Name"] == "coval-agent-sdk"
    assert package.read_text("top_level.txt").split() == ["coval_agent_sdk"]
    assert [
        (entry.name, entry.value)
        for entry in package.entry_points
        if entry.group == "console_scripts"
    ] == []
    assert (
        pathlib.Path(coval_agent_sdk.__file__).with_name("py.typed").is_file()
    )
    files = {str(path) for path in package.files or ()}
    assert not any(path.startswith(("tests/", "backend/")) for path in files)


def test_package_has_no_executable_entry_point(tmp_path):
    package_path = pathlib.Path(coval_agent_sdk.__file__).parent
    assert not (package_path / "__main__.py").exists()
    assert not (package_path / "_cli.py").exists()
    result = subprocess.run(
        [sys.executable, "-I", "-m", "coval_agent_sdk"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "cannot be directly executed" in result.stderr


@pytest.mark.parametrize(
    "url", ["file:///tmp/api", "ftp://example.test/api", ""]
)
def test_client_rejects_non_http_api_roots(url):
    with pytest.raises(ValueError, match="http or https"):
        coval_agent_sdk.CovalVoiceClient(
            api_key="synthetic-key", api_base_url=url
        )


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8081/v1", "https://api.coval.dev/v1"]
)
def test_client_accepts_http_and_https_api_roots(url):
    voice = coval_agent_sdk.CovalVoiceClient(
        api_key="synthetic-key", api_base_url=url
    )
    assert voice._api.api_base_url == url
    assert not hasattr(voice, "api")


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


def test_opentelemetry_dependencies_belong_only_to_tracing_extra():
    package = importlib.metadata.distribution("coval-agent-sdk")
    declared = [
        requirements.Requirement(item) for item in package.requires or ()
    ]

    def dependencies(extra):
        return {
            item.name
            for item in declared
            if item.marker is None or item.marker.evaluate({"extra": extra})
        }

    assert dependencies("") == {"coval-sdk", "urllib3", "websockets"}
    assert dependencies("tracing") == {
        "coval-sdk",
        "urllib3",
        "websockets",
        "opentelemetry-api",
        "opentelemetry-sdk",
        "opentelemetry-exporter-otlp-proto-http",
    }
    assert "tracing" in package.metadata.get_all("Provides-Extra")
