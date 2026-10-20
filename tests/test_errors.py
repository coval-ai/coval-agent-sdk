"""SDK failures share a catch boundary distinct from application errors."""

import asyncio

import pytest

import coval_agent_sdk


@pytest.mark.parametrize(
    "failure",
    [
        coval_agent_sdk.CovalError("SDK failed"),
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


@pytest.mark.parametrize(
    "error_type",
    [
        coval_agent_sdk.CovalHttpError,
        coval_agent_sdk.CovalConnectionError,
        coval_agent_sdk.CovalTraceError,
        coval_agent_sdk.RunError,
    ],
)
def test_specific_errors_inherit_directly_from_the_shared_base(error_type):
    assert error_type.__bases__ == (coval_agent_sdk.CovalError,)
