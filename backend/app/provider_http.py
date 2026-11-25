"""Bounded JSON responses from configured model providers."""
import json

import httpx

MAX_PROVIDER_RESPONSE_BYTES = 2_000_000


async def read_provider_json(response: httpx.Response) -> object:
    response.raise_for_status()
    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise ValueError("unsupported_provider_encoding")
    body = bytearray()
    async for chunk in response.aiter_raw():
        if len(chunk) > MAX_PROVIDER_RESPONSE_BYTES - len(body):
            raise ValueError("provider_response_limit")
        body.extend(chunk)
    return json.loads(body)
