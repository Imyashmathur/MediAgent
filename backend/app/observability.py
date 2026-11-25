"""Bounded, content-free local request metrics and correlation context."""
import contextvars
import threading
import time
import uuid

correlation_id = contextvars.ContextVar("correlation_id", default=None)
_lock = threading.Lock()
_responses = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "other": 0, "aborted": 0}
_latency = {"le_100ms": 0, "le_500ms": 0, "le_2000ms": 0, "gt_2000ms": 0}
_providers = {"chat": 0, "chat_validation": 0, "embedding": 0}


def provider_failure(kind: str) -> None:
    with _lock:
        _providers[kind] += 1


def snapshot() -> dict:
    with _lock:
        return {"responses": dict(_responses), "latency_buckets": dict(_latency), "provider_failures": dict(_providers)}


def _record(status: int | None, duration: float, complete: bool) -> None:
    category = "aborted" if not complete else f"{status // 100}xx" if status is not None and 200 <= status <= 599 else "other"
    bucket = "le_100ms" if duration <= 0.1 else "le_500ms" if duration <= 0.5 else "le_2000ms" if duration <= 2 else "gt_2000ms"
    with _lock:
        _responses[category] += 1
        _latency[bucket] += 1


class ObservabilityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        request_id = str(uuid.uuid4())
        token = correlation_id.set(request_id)
        started = time.monotonic()
        status = None
        recorded = False

        async def send_with_context(message):
            nonlocal status, recorded
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in (b"x-request-id", b"cache-control")]
                message = {**message, "headers": [*headers, (b"x-request-id", request_id.encode("ascii")), (b"cache-control", b"no-store")]}
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False) and not recorded:
                _record(status, time.monotonic() - started, True)
                recorded = True

        try:
            await self.app(scope, receive, send_with_context)
        finally:
            if scope["type"] == "http" and not recorded:
                _record(status, time.monotonic() - started, False)
            correlation_id.reset(token)
