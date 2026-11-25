"""Knowledge chunking and optional Ollama embedding provider."""
import math
import os
import re

import httpx
from .observability import provider_failure
from .provider_http import read_provider_json

DIMENSIONS = 384


class EmbeddingUnavailable(Exception):
    pass


def embedding_model() -> str:
    return os.getenv("OLLAMA_EMBED_MODEL", "all-minilm")


def canonical_model(name: str) -> str:
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


async def embedding_digest() -> str:
    """Resolve the configured model tag to the digest reported by Ollama."""
    base = os.getenv("OLLAMA_BASE_URL", "").strip().rstrip("/")
    model = embedding_model().strip()
    if not base or not model:
        provider_failure("embedding")
        raise EmbeddingUnavailable("Embedding provider is not configured")
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", f"{base}/api/tags", headers={"Accept-Encoding": "identity"}) as response:
                entries = (await read_provider_json(response))["models"]
            if not isinstance(entries, list):
                raise ValueError("Invalid model list")
            matches = [entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("name"), str) and canonical_model(entry["name"]) == canonical_model(model)]
            if len(matches) != 1 or not isinstance(matches[0].get("digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", matches[0]["digest"]):
                raise ValueError("Model digest unavailable")
            return matches[0]["digest"]
    except (httpx.HTTPError, httpx.StreamError, ValueError, KeyError, TypeError) as exc:
        provider_failure("embedding")
        raise EmbeddingUnavailable("Embedding model identity unavailable") from exc


def chunks(text: str, size: int = 600, overlap: int = 50) -> list[str]:
    text = text.strip()
    if not text:
        return []
    result = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind(" ", start + size // 2, end)
            if boundary > start:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            result.append(chunk)
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return result


async def embed_texts(texts: list[str]) -> list[list[float]]:
    base = os.getenv("OLLAMA_BASE_URL", "").strip().rstrip("/")
    if not base:
        provider_failure("embedding")
        raise EmbeddingUnavailable("Embedding provider is not configured")
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            vectors = []
            for offset in range(0, len(texts), 16):
                batch = texts[offset:offset + 16]
                async with client.stream("POST", f"{base}/api/embed", json={"model": embedding_model(), "input": batch, "truncate": False}, headers={"Accept-Encoding": "identity"}) as response:
                    data = await read_provider_json(response)
                batch_vectors = data["embeddings"]
                if len(batch_vectors) != len(batch):
                    raise ValueError("Wrong number of embeddings")
                for vector in batch_vectors:
                    if len(vector) != DIMENSIONS or any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) for x in vector) or not any(vector):
                        raise ValueError("Invalid embedding")
                    vectors.append(vector)
            return vectors
    except (httpx.HTTPError, httpx.StreamError, ValueError, KeyError, TypeError, OverflowError) as exc:
        provider_failure("embedding")
        raise EmbeddingUnavailable("Embedding provider unavailable or returned invalid vectors") from exc
