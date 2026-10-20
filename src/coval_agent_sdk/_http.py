"""Private Coval API SDK transport with JSON and protobuf request support."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from typing import cast
from urllib import parse

from coval_sdk import api_client
from coval_sdk import configuration
from coval_sdk import exceptions
from urllib3 import exceptions as http_exceptions

from coval_agent_sdk import _defaults
from coval_agent_sdk import errors


class CovalApi:
    """Private adapter around the Coval API SDK request client."""

    def __init__(
        self,
        *,
        api_base_url: str,
        api_key: str,
        workspace_id: str | None = None,
    ) -> None:
        """Configure the API root and authentication headers.

        Args:
            api_base_url: API root including the version path.
            api_key: Organization API key.
            workspace_id: Optional workspace selector.
        """
        if parse.urlsplit(api_base_url).scheme not in {"http", "https"}:
            raise ValueError("api_base_url must use http or https.")
        self.api_base_url = api_base_url.rstrip("/")
        self.api_key = api_key
        self.workspace_id = workspace_id

    async def get(
        self, path: str, *, query: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Fetch an object resource without blocking the audio event loop.

        Args:
            path: Resource path relative to the configured API root.
            query: Optional URL query parameters.

        Returns:
            The decoded JSON object.

        Raises:
            errors.TunnelError: The request fails or returns non-object JSON.
        """
        return await asyncio.to_thread(
            self._request_json, "GET", path, query, None
        )

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Post JSON once without automatically retrying mutations.

        Args:
            path: Resource path relative to the configured API root.
            payload: JSON object to send.

        Returns:
            The decoded JSON response object.

        Raises:
            errors.TunnelError: The request fails or returns non-object JSON.
        """
        return await asyncio.to_thread(
            self._request_json, "POST", path, None, payload
        )

    def post_protobuf(
        self, path: str, payload: bytes, *, headers: dict[str, str]
    ) -> None:
        """Post protobuf once from a worker thread, releasing request resources.

        Args:
            path: Resource path relative to the configured API root.
            payload: Encoded protobuf request body.
            headers: Additional endpoint-specific headers.

        Raises:
            errors.TunnelError: The API rejects or cannot receive the request.
        """
        self._request(
            "POST",
            path,
            None,
            payload,
            content_type="application/x-protobuf",
            headers=headers,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body = self._request(method, path, query, payload)
        try:
            response_payload: object = json.loads(body)
        except (ValueError, UnicodeError):
            raise errors.TunnelError(
                f"Coval {method} {path} returned invalid JSON."
            ) from None
        if not isinstance(response_payload, dict):
            raise errors.TunnelError(
                f"Coval {method} {path} returned a non-object JSON response."
            )
        return cast(dict[str, Any], response_payload)

    def _request(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None,
        payload: dict[str, Any] | bytes | None,
        *,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> bytes:
        settings = configuration.Configuration(
            host=self.api_base_url,
            api_key={"Coval_Agents_API_ApiKeyAuth": self.api_key},
            retries=0,
        )
        # The generated client leaves request/response members untyped. Keep
        # that dynamic boundary here and validate JSON before returning it.
        sdk: Any = api_client.ApiClient(settings)
        request_headers = dict(headers or {})
        request_headers.update(
            {
                "Accept": "application/json",
                "Content-Type": content_type,
            }
        )
        if self.workspace_id is not None:
            request_headers["X-Coval-Workspace-Id"] = self.workspace_id
        try:
            # Discovery is not in the generated endpoint set. Keep request
            # serialization and transport in the API SDK; models stay private.
            parameters = sdk.param_serialize(
                method=method,
                resource_path=f"/{path.lstrip('/')}",
                query_params=list((query or {}).items()),
                header_params=request_headers,
                body=payload,
                auth_settings=["Coval_Agents_API_ApiKeyAuth"],
            )
            response = sdk.call_api(
                *parameters,
                _request_timeout=_defaults.HTTP_TIMEOUT_SECONDS,
            )
            try:
                body = response.read()
                if not 200 <= response.status <= 299:
                    detail = body[:500].decode("utf-8", errors="replace")
                    raise errors.CovalHttpError(
                        status_code=response.status,
                        message=(
                            f"Coval {method} {path} returned HTTP "
                            f"{response.status}: {detail}"
                        ),
                    )
                return cast(bytes, body)
            finally:
                response.response.close()
                response.response.release_conn()
        except exceptions.ApiException as exception:
            status: object = cast(Any, exception).status
            if isinstance(status, int) and status != 0:
                raise errors.CovalHttpError(
                    status_code=status,
                    message=(f"Coval {method} {path} returned HTTP {status}."),
                ) from None
            raise errors.CovalConnectionError(
                f"Coval {method} {path} could not reach the API."
            ) from None
        except (http_exceptions.HTTPError, OSError) as exception:
            raise errors.CovalConnectionError(
                f"Coval {method} {path} could not reach the API: "
                f"{type(exception).__name__}."
            ) from None
        finally:
            # A cancelled coroutine cannot stop its blocking worker. The worker
            # retains ownership and releases its pool after the bounded request.
            sdk.rest_client.pool_manager.clear()
