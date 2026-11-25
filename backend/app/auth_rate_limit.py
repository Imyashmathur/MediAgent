"""Atomic Redis limits for public authentication endpoints."""
import hashlib
import hmac
import os

import redis.asyncio as redis
from redis.exceptions import RedisError

SCRIPT = """
local a = redis.call('INCR', KEYS[1])
if a == 1 then redis.call('EXPIRE', KEYS[1], 60) end
local b = redis.call('INCR', KEYS[2])
if b == 1 then redis.call('EXPIRE', KEYS[2], 60) end
return {a, b}
"""


class RateLimitUnavailable(Exception):
    pass


async def redis_available() -> bool:
    client = redis.from_url(os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"), socket_connect_timeout=2, socket_timeout=2)
    try:
        return bool(await client.ping())
    except (RedisError, OSError):
        return False
    finally:
        await client.aclose()


async def check(operation: str, address: str, identity: str, identity_limit: int, address_limit: int) -> bool:
    # A keyed digest makes guessing an email or IP from a Redis key harder.
    # All API instances must share the same secret for shared rate limits.
    secret = os.getenv("JWT_SECRET", "local-development-secret-change-me").encode()
    def digest(kind: str, value: str) -> str:
        return hmac.new(secret, f"auth-rate:{kind}:{value}".encode(), hashlib.sha256).hexdigest()

    client = redis.from_url(os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"), socket_connect_timeout=2, socket_timeout=2)
    try:
        counts = await client.eval(SCRIPT, 2, f"auth-rate:{operation}:identity:{digest('identity', identity)}", f"auth-rate:{operation}:address:{digest('address', address)}")
        return counts[0] <= identity_limit and counts[1] <= address_limit
    except (RedisError, OSError) as exc:
        raise RateLimitUnavailable from exc
    finally:
        await client.aclose()
