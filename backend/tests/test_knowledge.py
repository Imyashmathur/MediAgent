import json

import httpx
import pytest

from app import knowledge


class JsonStream(httpx.AsyncByteStream):
    def __init__(self, value): self.value = value
    async def __aiter__(self): yield json.dumps(self.value).encode()


def json_response(value):
    return httpx.Response(200, stream=JsonStream(value))


def test_chunking_preserves_text_and_bounded_size():
    text = "Synthetic guideline text. " * 100
    parts = knowledge.chunks(text)
    assert len(parts) > 1 and all(len(part) <= 600 for part in parts)
    assert "Synthetic guideline" in parts[0]


@pytest.mark.asyncio
async def test_ollama_model_digest_requires_unique_valid_match(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://provider.test")
    monkeypatch.setenv("OLLAMA_EMBED_MODEL", "synthetic")
    real_client = httpx.AsyncClient
    models = [{"name":"synthetic:latest", "digest":"a"*64}]
    def handler(request):
        assert request.url.path == "/api/tags"
        return json_response({"models":models})
    monkeypatch.setattr(knowledge.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    assert await knowledge.embedding_digest() == "a"*64
    models.append({"name":"synthetic", "digest":"b"*64})
    with pytest.raises(knowledge.EmbeddingUnavailable): await knowledge.embedding_digest()
    models[:] = [{"name":"synthetic", "digest":"invalid"}]
    with pytest.raises(knowledge.EmbeddingUnavailable): await knowledge.embedding_digest()


@pytest.mark.asyncio
async def test_ollama_contract_and_invalid_embedding(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://provider.test")
    monkeypatch.setenv("OLLAMA_EMBED_MODEL", "test-model")
    real_client = httpx.AsyncClient
    def handler(request):
        assert request.url.path == "/api/embed"
        data = json.loads(request.content)
        assert data == {"model": "test-model", "input": ["synthetic"], "truncate": False}
        return json_response({"model": "test-model", "embeddings": [[1.0] + [0.0] * 383]})
    monkeypatch.setattr(knowledge.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    assert len((await knowledge.embed_texts(["synthetic"]))[0]) == 384
    def invalid(request):
        return json_response({"embeddings": [[0.0] * 384]})
    monkeypatch.setattr(knowledge.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(invalid), **kwargs))
    with pytest.raises(knowledge.EmbeddingUnavailable):
        await knowledge.embed_texts(["synthetic"])
