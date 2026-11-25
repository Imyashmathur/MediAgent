import pytest

from app import assistant_provider, knowledge, observability


@pytest.mark.asyncio
async def test_metrics_count_completed_and_aborted_requests_without_payloads():
    before = observability.snapshot()
    sent = []
    async def send(message):
        sent.append(message)
    async def app(scope, receive, send):
        assert observability.correlation_id.get()
        await send({"type": "http.response.start", "status": 204, "headers": [(b"Cache-Control", b"public, max-age=3600")]})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    middleware = observability.ObservabilityMiddleware(app)
    await middleware({"type": "http", "path": "/sensitive-synthetic-path"}, None, send)
    assert observability.correlation_id.get() is None
    assert dict(sent[0]["headers"])[b"x-request-id"]
    assert [value for name, value in sent[0]['headers'] if name.lower() == b'cache-control'] == [b'no-store']
    async def failed(scope, receive, send):
        raise RuntimeError("synthetic sensitive exception")
    with pytest.raises(RuntimeError):
        await observability.ObservabilityMiddleware(failed)({"type": "http"}, None, send)
    after = observability.snapshot()
    assert after["responses"]["2xx"] == before["responses"]["2xx"] + 1
    assert after["responses"]["aborted"] == before["responses"]["aborted"] + 1
    assert sum(after["latency_buckets"].values()) == sum(before["latency_buckets"].values()) + 2
    assert "sensitive" not in str(after)


@pytest.mark.asyncio
async def test_provider_failure_metrics_are_bounded_and_content_free(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    before = observability.snapshot()["provider_failures"]
    with pytest.raises(knowledge.EmbeddingUnavailable):
        await knowledge.embed_texts(["synthetic sensitive text"])
    with pytest.raises(assistant_provider.AssistantUnavailable):
        await assistant_provider._chat([], {})
    after = observability.snapshot()["provider_failures"]
    assert after["embedding"] == before["embedding"] + 1
    assert after["chat"] == before["chat"] + 1
    assert "synthetic" not in str(after)
