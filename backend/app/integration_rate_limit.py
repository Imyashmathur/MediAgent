"""Redis fixed-window limits for scoped integration calls; fail closed on outage."""
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


async def check_rate_limit(credential_id: str, user_id: str) -> bool:
    url = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    client = redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    try:
        counts = await client.eval(SCRIPT, 2, f"mcp-rate:credential:{credential_id}", f"mcp-rate:user:{user_id}")
        return counts[0] <= 30 and counts[1] <= 60
    except (RedisError, OSError) as exc:
        raise RateLimitUnavailable from exc
    finally:
        await client.aclose()
