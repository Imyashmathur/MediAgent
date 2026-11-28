import json

import httpx
import pytest

from app import assistant_provider, knowledge, provider_http


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["chat", "tags", "embed"])
@pytest.mark.parametrize("failure", ["oversized", "compressed", "invalid_json", "http_error"])
async def test_provider_failures_are_bounded_and_sanitized(monkeypatch, endpoint, failure):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://provider.test")
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "synthetic")
    monkeypatch.setenv("OLLAMA_EMBED_MODEL", "synthetic")
    stream = Chunks([b'x' * 1_500_000, b'x' * 600_001] if failure == "oversized" else [b'{invalid synthetic secret'])
    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(503 if failure == "http_error" else 200,
                              headers={"content-encoding": "gzip"} if failure == "compressed" else {}, stream=stream)
    real_client = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    operation = {"chat": lambda: assistant_provider._chat([], {}),
                 "tags": knowledge.embedding_digest,
                 "embed": lambda: knowledge.embed_texts(["synthetic"])}[endpoint]
    exception = assistant_provider.AssistantUnavailable if endpoint == "chat" else knowledge.EmbeddingUnavailable
    with pytest.raises(exception) as error:
        await operation()
    assert "synthetic secret" not in str(error.value)
    assert stream.closed


@pytest.mark.asyncio
async def test_stream_size_limit_checks_combined_chunks(monkeypatch):
    monkeypatch.setattr(provider_http, "MAX_PROVIDER_RESPONSE_BYTES", 8)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Chunks([b'{"x":', b' 1}'])))) as client:
        async with client.stream("GET", "http://provider.test") as response:
            assert await provider_http.read_provider_json(response) == {"x": 1}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Chunks([b'{"x":', b' 12}'])))) as client:
        async with client.stream("GET", "http://provider.test") as response:
            with pytest.raises(ValueError, match="provider_response_limit"):
                await provider_http.read_provider_json(response)


@pytest.mark.asyncio
async def test_chat_stream_success(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://provider.test")
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "synthetic")
    real_client = httpx.AsyncClient
    def handler(request):
        assert request.url.path == "/api/chat"
        return httpx.Response(200, stream=Chunks([json.dumps({"message": {"content": "ok"}}).encode()]))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    assert await assistant_provider._chat([], {}) == "ok"

@pytest.mark.asyncio
async def test_summary_chat_requests_disable_context_truncation(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://provider.test")
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "synthetic")
    observed = []
    real_client = httpx.AsyncClient
    def handler(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, stream=Chunks([json.dumps({"message": {"content": "ok"}}).encode()]))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    schema = {"properties": {"citations": {"items": {"enum": ["S1"]}}}}
    assert await assistant_provider._chat([], schema) == "ok"
    assert observed[0]["truncate"] is False and observed[0]["shift"] is False
    assert observed[0]["options"]["num_ctx"] == 16384
