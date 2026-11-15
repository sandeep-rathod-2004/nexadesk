import asyncio
import logging
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
from google import genai
from google.genai import errors, types

from app.core.config import Settings
from app.services.model_provider import (
    ModelUnavailableError,
    _build_messages,
)
from app.services.performance import log_stage

logger = logging.getLogger(__name__)
_shared_client: genai.Client | None = None
_TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class ModelRateLimitError(ModelUnavailableError):
    pass


def create_gemini_client(settings: Settings) -> genai.Client:
    if settings.gemini_api_key is None:
        raise ValueError("GEMINI_API_KEY is required for Gemini.")
    return genai.Client(
        api_key=settings.gemini_api_key.get_secret_value(),
        http_options=types.HttpOptions(
            timeout=int(settings.gemini_timeout_seconds * 1000),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def set_shared_gemini_client(client: genai.Client | None) -> None:
    global _shared_client
    _shared_client = client


@asynccontextmanager
async def _gemini_client(settings: Settings) -> AsyncIterator[Any]:
    if _shared_client is not None:
        yield _shared_client.aio
        return
    client = create_gemini_client(settings)
    async_client = client.aio
    try:
        yield async_client
    finally:
        await async_client.aclose()
        client.close()


def _contents(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "system":
            continue
        contents.append(
            {
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": message["content"]}],
            }
        )
    return contents


def _request_parts(
    message: str,
    history: list[dict[str, str]] | None,
    rag_context: str | None,
    request_id: UUID | str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    messages = _build_messages(message, history, rag_context, request_id)
    return messages[0]["content"], _contents(messages)


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, errors.APIError):
        return exc.code in _TRANSIENT_STATUS_CODES
    return isinstance(
        exc,
        (httpx.TimeoutException, httpx.TransportError, TimeoutError, OSError),
    )


def _raise_provider_error(exc: Exception) -> None:
    if isinstance(exc, errors.APIError) and exc.code == 429:
        raise ModelRateLimitError from exc
    raise ModelUnavailableError from exc


async def _retry_delay(attempt: int) -> None:
    await asyncio.sleep(min(0.25 * (2**attempt), 1.0))


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if not isinstance(text, str):
        return ""
    return text


class GeminiProvider:
    provider_name = "Gemini"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.model_name = settings.gemini_model
        self.performance_request_id: UUID | str | None = None
        self.last_usage: dict[str, int] | None = None

    def model_info(self) -> dict[str, str]:
        return {"provider": self.provider_name, "model": self.model_name}

    def _record_usage(self, response: Any) -> None:
        metadata = getattr(response, "usage_metadata", None)
        if metadata is None:
            return
        usage: dict[str, int] = {}
        for field, attr in (
            ("prompt_tokens", "prompt_token_count"),
            ("response_tokens", "candidates_token_count"),
            ("total_tokens", "total_token_count"),
        ):
            value = getattr(metadata, attr, None)
            if isinstance(value, int):
                usage[field] = value
        self.last_usage = usage or None

    async def generate(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
        rag_context: str | None = None,
    ) -> str:
        system_instruction, contents = _request_parts(
            message, history, rag_context
        )
        request_config = types.GenerateContentConfig(
            system_instruction=system_instruction
        )
        async with _gemini_client(self.settings) as client:
            for attempt in range(self.settings.gemini_max_retries + 1):
                try:
                    response = await client.models.generate_content(
                        model=self.model_name,
                        contents=contents,
                        config=request_config,
                    )
                    self._record_usage(response)
                    return _response_text(response).strip()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not _is_transient(exc) or attempt >= self.settings.gemini_max_retries:
                        _raise_provider_error(exc)
                    await _retry_delay(attempt)
        raise ModelUnavailableError

    async def stream(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
        rag_context: str | None = None,
    ) -> AsyncIterator[str]:
        request_id = self.performance_request_id
        system_instruction, contents = _request_parts(
            message, history, rag_context, request_id
        )
        request_config = types.GenerateContentConfig(
            system_instruction=system_instruction
        )
        started_at = time.perf_counter()
        log_stage("gemini_request_started", started_at, request_id)
        async with _gemini_client(self.settings) as client:
            for attempt in range(self.settings.gemini_max_retries + 1):
                emitted = False
                stream = None
                try:
                    stream = await client.models.generate_content_stream(
                        model=self.model_name,
                        contents=contents,
                        config=request_config,
                    )
                    async for response in stream:
                        text = _response_text(response)
                        self._record_usage(response)
                        if text:
                            if not emitted:
                                log_stage(
                                    "gemini_first_token_received",
                                    started_at,
                                    request_id,
                                )
                                emitted = True
                            yield text
                    return
                except asyncio.CancelledError:
                    raise
                except GeneratorExit:
                    raise
                except Exception as exc:
                    if (
                        emitted
                        or not _is_transient(exc)
                        or attempt >= self.settings.gemini_max_retries
                    ):
                        _raise_provider_error(exc)
                    await _retry_delay(attempt)
                finally:
                    if stream is not None:
                        try:
                            await stream.aclose()
                        except Exception:
                            logger.warning("Gemini stream cleanup failed.")


class GeminiEmbeddingProvider:
    provider_name = "Gemini"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.model_name = settings.configured_embedding_model

    async def embed_texts(
        self,
        texts: list[str],
        request_id: UUID | str | None = None,
        task_type: str | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []
        from app.services.embeddings import EmbeddingUnavailableError

        started_at = time.perf_counter()
        config = types.EmbedContentConfig(
            task_type=task_type or "SEMANTIC_SIMILARITY",
            output_dimensionality=self.settings.gemini_embedding_dimensions,
        )
        try:
            async with _gemini_client(self.settings) as client:
                for attempt in range(self.settings.gemini_max_retries + 1):
                    try:
                        response = await client.models.embed_content(
                            model=self.model_name,
                            contents=texts,
                            config=config,
                        )
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if (
                            not _is_transient(exc)
                            or attempt >= self.settings.gemini_max_retries
                        ):
                            if isinstance(exc, errors.APIError) and exc.code == 429:
                                raise EmbeddingUnavailableError(
                                    "The configured embedding provider is rate limited."
                                ) from exc
                            raise EmbeddingUnavailableError(
                                "The configured embedding provider is unavailable."
                            ) from exc
                        await _retry_delay(attempt)
        except asyncio.CancelledError:
            raise
        except EmbeddingUnavailableError:
            raise
        except Exception as exc:
            raise EmbeddingUnavailableError(
                "The configured embedding provider is unavailable."
            ) from exc

        embeddings = getattr(response, "embeddings", None)
        vectors: list[list[float]] = []
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise EmbeddingUnavailableError(
                "The embedding provider returned an invalid response."
            )
        for embedding in embeddings:
            values = getattr(embedding, "values", None)
            if not isinstance(values, list) or len(values) != self.settings.gemini_embedding_dimensions:
                raise EmbeddingUnavailableError(
                    "The embedding provider returned an incompatible vector."
                )
            try:
                vector = [float(value) for value in values]
            except (TypeError, ValueError) as exc:
                raise EmbeddingUnavailableError(
                    "The embedding provider returned an invalid vector."
                ) from exc
            if any(not math.isfinite(value) for value in vector):
                raise EmbeddingUnavailableError(
                    "The embedding provider returned an invalid vector."
                )
            vectors.append(vector)
        if request_id is not None:
            log_stage("query_embedding_completed", started_at, request_id)
        return vectors
