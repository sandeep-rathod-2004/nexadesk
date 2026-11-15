import math
import time
from uuid import UUID

import httpx

from app.core.config import Settings
from app.services.model_provider import ollama_client, ollama_timeout
from app.services.performance import log_stage


class EmbeddingUnavailableError(Exception):
    pass


async def _embed_ollama_texts(
    settings: Settings,
    texts: list[str],
    request_id: UUID | str | None = None,
    task_type: str | None = None,
) -> list[list[float]]:
    if not texts:
        return []
    started_at = time.perf_counter()
    try:
        async with ollama_client(settings) as client:
            response = await client.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/embed",
                json={
                    "model": settings.configured_embedding_model,
                    "input": texts,
                    "keep_alive": settings.ollama_embedding_keep_alive,
                },
                timeout=ollama_timeout(settings),
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise EmbeddingUnavailableError from exc

    vectors = payload.get("embeddings") if isinstance(payload, dict) else None
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise EmbeddingUnavailableError("The embedding service returned an invalid response.")
    result: list[list[float]] = []
    dimension: int | None = None
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise EmbeddingUnavailableError("The embedding service returned an invalid vector.")
        if dimension is None:
            dimension = len(vector)
        if len(vector) != dimension:
            raise EmbeddingUnavailableError("Embedding vector dimensions do not match.")
        try:
            values = [float(value) for value in vector]
        except (TypeError, ValueError) as exc:
            raise EmbeddingUnavailableError("The embedding service returned an invalid vector.") from exc
        if any(not math.isfinite(value) for value in values):
            raise EmbeddingUnavailableError("The embedding service returned a non-finite vector.")
        result.append(values)
    if request_id is not None:
        log_stage("query_embedding_completed", started_at, request_id)
    return result


async def embed_texts(
    settings: Settings,
    texts: list[str],
    request_id: UUID | str | None = None,
    task_type: str | None = None,
) -> list[list[float]]:
    from app.services.model_provider import create_embedding_provider

    provider = create_embedding_provider(settings)
    return await provider.embed_texts(texts, request_id, task_type)


def embedding_identity(settings: Settings) -> tuple[str, str]:
    return settings.embedding_provider, settings.configured_embedding_model
