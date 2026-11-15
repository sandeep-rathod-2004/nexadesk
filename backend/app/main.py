import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.api.routes.auth import router as auth_router
from app.api.routes.chat import router as chat_router
from app.api.routes.conversations import router as conversations_router
from app.api.routes.knowledge import router as knowledge_router
from app.api.routes.storage_cleanup import router as storage_cleanup_router
from app.api.routes.workspaces import router as workspaces_router
from app.core.config import get_settings
from app.services.model_provider import set_shared_ollama_client
from app.services.generation_coordination import (
    LocalGenerationCoordinator,
    RedisGenerationCoordinator,
)
from app.services.gemini_provider import (
    create_gemini_client,
    set_shared_gemini_client,
)
from app.services.performance import request_id_for_log

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    coordinator = None
    if settings.generation_coordination_backend == "redis":
        if not settings.redis_url:
            raise RuntimeError("REDIS_URL is required for Redis generation coordination.")
        redis_client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        try:
            await redis_client.ping()
        except Exception:
            await redis_client.aclose()
            logging.getLogger(__name__).error(
                "Redis generation coordination could not be initialized."
            )
            raise RuntimeError("Redis generation coordination is unavailable.") from None
        coordinator = RedisGenerationCoordinator(
            redis_client,
            lease_seconds=settings.generation_lease_seconds,
            poll_seconds=settings.generation_coordination_poll_seconds,
        )
    else:
        coordinator = LocalGenerationCoordinator()
    _app.state.generation_coordinator = coordinator
    gemini_client = None
    try:
        if settings.ai_provider == "gemini" or settings.embedding_provider == "gemini":
            gemini_client = create_gemini_client(settings)
            set_shared_gemini_client(gemini_client)
        async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            timeout=None,
        ) as client:
            set_shared_ollama_client(client)
            try:
                yield
            finally:
                set_shared_ollama_client(None)
    finally:
        if gemini_client is not None:
            set_shared_gemini_client(None)
            await gemini_client.aio.aclose()
            gemini_client.close()
        await coordinator.close()


fastapi_app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Local-first chatbot platform development API.",
    lifespan=lifespan,
)
fastapi_app.include_router(chat_router, prefix=settings.api_v1_prefix)
fastapi_app.include_router(auth_router, prefix=settings.api_v1_prefix)
fastapi_app.include_router(workspaces_router, prefix=settings.api_v1_prefix)
fastapi_app.include_router(conversations_router, prefix=settings.api_v1_prefix)
fastapi_app.include_router(knowledge_router, prefix=settings.api_v1_prefix)
fastapi_app.include_router(storage_cleanup_router, prefix=settings.api_v1_prefix)


@fastapi_app.get("/", tags=["system"])
async def root():
    return {"name": settings.app_name, "status": "running", "version": "0.1.0"}


@fastapi_app.get("/health", tags=["system"])
async def health():
    return {"status": "healthy"}


@fastapi_app.get("/ready", tags=["system"])
async def ready():
    return {"status": "ready"}


@fastapi_app.middleware("http")
async def log_request_start(request, call_next):
    request.state.request_received_at = time.perf_counter()
    request_id = request_id_for_log(request.headers.get("x-request-id"))
    if request.url.path.endswith("/chat/stream"):
        logging.getLogger("app.chat.performance").info(
            json.dumps(
                {
                    "event": "chat_performance",
                    "stage": "request_received",
                    "request_id": request_id,
                },
                separators=(",", ":"),
            )
        )
    return await call_next(request)


# Wrapping the complete FastAPI app also adds CORS headers to generated error responses.
app = CORSMiddleware(
    app=fastapi_app,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)
