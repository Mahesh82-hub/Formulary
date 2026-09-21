import asyncio
from typing import Any, cast

import httpx

from app.llm.provider import LLMProviderError


class HTTPModelProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
        max_retries: int = 1,
        retry_base_delay_seconds: float = 0.25,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/") + "/"
        self._timeout_seconds = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._retry_base_delay_seconds = max(0.0, retry_base_delay_seconds)
        self._transport = transport

    async def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=self._base_url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=self._timeout_seconds,
                    transport=self._transport,
                ) as client:
                    response = await client.post(path.lstrip("/"), json=payload)
                    response.raise_for_status()
                break
            except httpx.TimeoutException as error:
                if attempt < self._max_retries:
                    await self._retry_delay(attempt)
                    continue
                raise LLMProviderError(
                    "The model provider timed out",
                    code="provider_timeout",
                    retryable=True,
                    retry_count=attempt,
                ) from error
            except httpx.HTTPStatusError as error:
                status_code = error.response.status_code
                retryable = status_code == 429 or status_code >= 500
                if retryable and attempt < self._max_retries:
                    await self._retry_delay(attempt)
                    continue
                raise self._http_status_error(error.response, attempt) from error
            except httpx.HTTPError as error:
                if attempt < self._max_retries:
                    await self._retry_delay(attempt)
                    continue
                raise LLMProviderError(
                    "The model provider could not be reached",
                    code="provider_unreachable",
                    retryable=True,
                    retry_count=attempt,
                ) from error

        if response is None:
            raise LLMProviderError(
                "The model provider returned no response",
                code="provider_invalid_response",
            )
        try:
            data: object = response.json()
        except ValueError as error:
            raise LLMProviderError(
                "The model provider returned invalid JSON",
                code="provider_invalid_response",
                request_id=self._request_id(response),
            ) from error
        if not isinstance(data, dict):
            raise LLMProviderError(
                "The model provider returned an invalid response",
                code="provider_invalid_response",
                request_id=self._request_id(response),
            )
        return cast(dict[str, Any], data)

    async def _retry_delay(self, attempt: int) -> None:
        delay = self._retry_base_delay_seconds * (2**attempt)
        if delay:
            await asyncio.sleep(delay)

    def _http_status_error(self, response: httpx.Response, retry_count: int) -> LLMProviderError:
        status_code = response.status_code
        provider_details = self._provider_error_details(response)
        provider_message = provider_details.get("provider_message")
        if status_code == 429:
            message = "The model provider rate limit was reached"
            code = "provider_rate_limited"
            retryable = True
        elif status_code >= 500:
            message = f"The model provider is temporarily unavailable (HTTP {status_code})"
            code = "provider_unavailable"
            retryable = True
        elif status_code == 413:
            message = "The model provider rejected the request as too large (HTTP 413)"
            code = "provider_payload_too_large"
            retryable = False
        else:
            message = f"The model provider rejected the request (HTTP {status_code})"
            code = "provider_request_rejected"
            retryable = False
        if isinstance(provider_message, str):
            message = f"{message}: {provider_message}"
        return LLMProviderError(
            message,
            code=code,
            retryable=retryable,
            status_code=status_code,
            request_id=self._request_id(response),
            retry_count=retry_count,
            details=provider_details,
        )

    @staticmethod
    def _provider_error_details(response: httpx.Response) -> dict[str, Any]:
        try:
            payload: object = response.json()
        except ValueError:
            return {}
        if not isinstance(payload, dict):
            return {}
        raw_error = payload.get("error")
        if not isinstance(raw_error, dict):
            return {}
        details: dict[str, Any] = {}
        for source_key, target_key in (
            ("message", "provider_message"),
            ("type", "provider_error_type"),
            ("code", "provider_error_code"),
            ("param", "provider_error_parameter"),
        ):
            value = raw_error.get(source_key)
            if isinstance(value, str) and value:
                details[target_key] = value[:500]
        return details

    @staticmethod
    def _request_id(response: httpx.Response) -> str | None:
        for header in (
            "x-request-id",
            "x-groq-request-id",
            "openai-request-id",
            "request-id",
            "cf-ray",
        ):
            value = response.headers.get(header)
            if isinstance(value, str) and value:
                return value[:255]
        return None


def object_list(value: object, *, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise LLMProviderError(
            f"The model provider response is missing {field}",
            code="provider_invalid_response",
        )
    objects: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise LLMProviderError(
                f"The model provider returned invalid {field}",
                code="provider_invalid_response",
            )
        objects.append(cast(dict[str, Any], item))
    return objects


def string_value(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise LLMProviderError(
            f"The model provider response is missing {field}",
            code="provider_invalid_response",
        )
    return value


def json_object(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LLMProviderError(
            f"The model provider response contains invalid {field}",
            code="provider_invalid_response",
        )
    return cast(dict[str, Any], value)
