import os
import pytest
from httpx import ASGITransport, AsyncClient

from app import auth_rate_limit
from app.main import app


@pytest.mark.asyncio
async def test_authentication_fails_closed_and_reports_limits(monkeypatch):
    async def limited(*args):
        return False

    monkeypatch.setattr(auth_rate_limit, "check", limited)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/auth/login", json={"email": "synthetic@example.test", "password": "wrong"})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"

        async def unavailable(*args):
            raise auth_rate_limit.RateLimitUnavailable()

        monkeypatch.setattr(auth_rate_limit, "check", unavailable)
        response = await client.post("/auth/login", json={"email": "synthetic@example.test", "password": "wrong"})
        assert response.status_code == 503


@pytest.mark.asyncio
async def test_redis_keys_do_not_contain_authentication_inputs(monkeypatch):
    import hashlib
    observed = []
    class RedisStub:
        async def eval(self, script, key_count, *keys):
            assert key_count == 2
            assert all("synthetic@example.test" not in key and "192.0.2.1" not in key for key in keys)
            observed.append(keys)
            return [11, 1]

        async def aclose(self):
            pass

    monkeypatch.setattr(auth_rate_limit.redis, "from_url", lambda *args, **kwargs: RedisStub())
    monkeypatch.setenv("JWT_SECRET", "synthetic-shared-secret")
    assert not await auth_rate_limit.check("login", "192.0.2.1", "synthetic@example.test", 10, 100)
    assert not await auth_rate_limit.check("login", "192.0.2.1", "synthetic@example.test", 10, 100)
    assert observed[0] == observed[1]
    assert hashlib.sha256(b"synthetic@example.test").hexdigest() not in observed[0][0]
    monkeypatch.setenv("JWT_SECRET", "different-secret")
    await auth_rate_limit.check("login", "192.0.2.1", "synthetic@example.test", 10, 100)
    assert observed[0] != observed[2]


@pytest.mark.skipif(os.getenv('MEDIAGENT_REDIS_TEST') != '1', reason='requires local Redis')
@pytest.mark.asyncio
async def test_concurrent_redis_writes_enforce_the_shared_identity_limit():
    import asyncio
    import uuid
    identity=f"synthetic-{uuid.uuid4()}"
    results=await asyncio.gather(*(auth_rate_limit.check("synthetic-write-test","synthetic-peer",identity,3,1000) for _ in range(12)))
    assert sum(results) == 3
