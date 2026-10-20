"""SDK failures share a catch boundary distinct from application errors."""

import asyncio

import pytest

import coval_agent_sdk


@pytest.mark.parametrize(
    "failure",
    [
        coval_agent_sdk.CovalError("SDK failed"),
        coval_agent_sdk.TunnelError("relay failed"),
        coval_agent_sdk.CovalHttpError(status_code=403, message="denied"),
        coval_agent_sdk.CovalConnectionError("unreachable"),
        coval_agent_sdk.CovalTraceError("upload failed"),
        coval_agent_sdk.RunError(
            "run failed", result=coval_agent_sdk.RunResult("run-a", None, ())
        ),
    ],
)
def test_shared_base_catches_existing_specific_errors(failure):
    with pytest.raises(coval_agent_sdk.CovalError) as caught:
        raise failure
    assert caught.value is failure
    assert isinstance(failure, RuntimeError)


@pytest.mark.parametrize(
    "failure",
    [ValueError("adapter"), RuntimeError("adapter"), asyncio.CancelledError()],
)
def test_application_failures_are_outside_sdk_error_hierarchy(failure):
    assert not isinstance(failure, coval_agent_sdk.CovalError)
