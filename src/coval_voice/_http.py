"""Authenticated JSON requests, isolated from voice session scheduling."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from typing import cast
from urllib import error
from urllib import parse
from urllib import request

from coval_voice import _defaults
from coval_voice import errors


class CovalApi:
    """Small HTTP client for the MVP, independent of generated SDK models."""

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
        return await asyncio.to_thread(self._request, "GET", path, query, None)

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
            self._request, "POST", path, None, payload
        )

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
        body = (
            json.dumps(payload).encode("utf-8") if payload is not None else None
        )
        headers = {
            "x-api-key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.workspace_id is not None:
            headers["X-Coval-Workspace-Id"] = self.workspace_id
        api_request = request.Request(  # noqa: S310 - HTTP(S) validated above.
            url,
            data=body,
            method=method,
            headers=headers,
        )
        try:
            with request.urlopen(  # noqa: S310 - HTTP(S) validated above.
                api_request, timeout=_defaults.HTTP_TIMEOUT_SECONDS
            ) as response:
                response_payload = json.load(response)
        except error.HTTPError as exception:
            response_body = exception.read(500).decode(
                "utf-8", errors="replace"
            )
            raise errors.CovalHttpError(
                status_code=exception.code,
                message=(
                    f"Coval {method} {path} returned HTTP "
                    f"{exception.code}: {response_body}"
                ),
            ) from exception
        except error.URLError as exception:
            raise errors.CovalConnectionError(
                f"Coval {method} {path} could not reach "
                f"{url}: {exception.reason}"
            ) from exception
        if not isinstance(response_payload, dict):
            raise errors.TunnelError(
                f"Coval {method} {path} returned a non-object JSON response."
            )
        return cast(dict[str, Any], response_payload)
