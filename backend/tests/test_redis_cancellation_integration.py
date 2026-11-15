import asyncio
import os
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401
from app.api.deps import get_db
from app.api.routes.auth import router as auth_router
from app.api.routes.chat import router as chat_router
from app.core.config import Settings
from app.db.base import Base
from app.models.chat_generation import ChatGeneration
from app.models.conversation import Message
from app.models.workspace import WorkspaceMembership
from app.services.generation_coordination import (
    GenerationCoordinationError,
    RedisGenerationCoordinator,
    get_generation_coordinator,
)

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL")


def _redis_url() -> str:
    if not TEST_REDIS_URL:
        pytest.skip("TEST_REDIS_URL is not configured.")
    if not TEST_REDIS_URL.startswith(("redis://127.0.0.1:", "redis://localhost:")):
        pytest.fail("Redis integration tests require a loopback test Redis URL.")
    return TEST_REDIS_URL


def _coordinator(url: str, lease_seconds: int = 15) -> RedisGenerationCoordinator:
    return RedisGenerationCoordinator(
        Redis.from_url(url, decode_responses=True),
        lease_seconds=lease_seconds,
        poll_seconds=0.05,
    )


def test_redis_shared_state_authorization_completion_expiry_and_failure():
    async def exercise():
        url = _redis_url()
        redis_a = Redis.from_url(url, decode_responses=True)
        redis_b = Redis.from_url(url, decode_responses=True)
        coordinator_a = _coordinator(url)
        coordinator_b = _coordinator(url)
        generation_id, user_id, workspace_id = uuid4(), uuid4(), uuid4()
        try:
            lease = await coordinator_a.register(
                generation_id, user_id, workspace_id
            )
            key = coordinator_a._key(generation_id)
            state = await redis_a.hgetall(key)
            assert state == {
                "user_id": str(user_id),
                "workspace_id": str(workspace_id),
                "owner_id": lease.owner_id,
                "status": "running",
            }
            assert "prompt" not in state and "private document" not in repr(state)
            assert await coordinator_b.workspace_for(generation_id, uuid4()) is None
            assert await coordinator_b.request_cancel(
                generation_id, uuid4(), workspace_id
            ) is False
            assert await coordinator_b.request_cancel(
                generation_id, user_id, uuid4()
            ) is False
            assert await coordinator_b.request_cancel(
                uuid4(), user_id, workspace_id
            ) is False
            assert await coordinator_b.request_cancel(
                generation_id, user_id, workspace_id
            ) is True
            assert await coordinator_b.request_cancel(
                generation_id, user_id, workspace_id
            ) is True
            await asyncio.wait_for(lease.cancel_requested.wait(), timeout=2)
            assert await coordinator_a.begin_completion(lease) is False
            await coordinator_a.finish(lease)
            assert await redis_a.exists(key) == 0

            completed_id = uuid4()
            completed_lease = await coordinator_a.register(
                completed_id, user_id, workspace_id
            )
            assert await coordinator_a.begin_completion(completed_lease) is True
            assert await coordinator_b.request_cancel(
                completed_id, user_id, workspace_id
            ) is False
            await coordinator_a.finish(completed_lease)

            expired_id = uuid4()
            expired_lease = await coordinator_a.register(
                expired_id, user_id, workspace_id
            )
            assert expired_lease.monitor_task is not None
            expired_lease.monitor_task.cancel()
            await asyncio.gather(
                expired_lease.monitor_task, return_exceptions=True
            )
            await redis_a.pexpire(coordinator_a._key(expired_id), 100)
            await asyncio.sleep(0.2)
            assert await redis_a.exists(coordinator_a._key(expired_id)) == 0
            await coordinator_a.finish(expired_lease)

            shutdown_id = uuid4()
            shutdown_lease = await coordinator_a.register(
                shutdown_id, user_id, workspace_id
            )
            shutdown_key = coordinator_a._key(shutdown_id)
            await coordinator_a._client.aclose()
            await asyncio.wait_for(
                shutdown_lease.cancel_requested.wait(), timeout=2
            )
            # Set the short TTL only after the owner monitor loses its connection;
            # otherwise it can renew the lease just before shutdown is observed.
            await redis_b.pexpire(shutdown_key, 100)
            await asyncio.sleep(0.2)
            assert await redis_a.exists(shutdown_key) == 0
        finally:
            await coordinator_a.close()
            await coordinator_b.close()
            await redis_a.aclose()
            await redis_b.aclose()

    asyncio.run(exercise())


def test_redis_unavailable_fails_closed():
    async def exercise():
        coordinator = _coordinator("redis://127.0.0.1:1/15")
        with pytest.raises(GenerationCoordinationError):
            await coordinator.register(uuid4(), uuid4(), uuid4())
        await coordinator.close()

    asyncio.run(exercise())


def test_two_fastapi_instances_cancel_owner_stream_and_persist_interruption(
    tmp_path, monkeypatch
):
    url = _redis_url()
    database_path = tmp_path / "redis-cancellation.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    coordinator_a = _coordinator(url)
    coordinator_b = _coordinator(url)

    def create_instance(coordinator):
        @asynccontextmanager
        async def lifespan(_application):
            yield
            await coordinator.close()

        application = FastAPI(lifespan=lifespan)
        application.include_router(auth_router, prefix="/api/v1")
        application.include_router(chat_router, prefix="/api/v1")
        application.dependency_overrides[get_db] = override_db

        def override_coordinator():
            return coordinator

        application.dependency_overrides[get_generation_coordinator] = (
            override_coordinator
        )
        return application

    app_a = create_instance(coordinator_a)
    app_b = create_instance(coordinator_b)

    provider_closed = threading.Event()
    provider_waiting = threading.Event()

    class GeminiModels:
        async def generate_content_stream(self, **_kwargs):
            async def chunks():
                try:
                    yield SimpleNamespace(text="partial output", usage_metadata=None)
                    provider_waiting.set()
                    await asyncio.Event().wait()
                finally:
                    provider_closed.set()

            return chunks()

    from app.api.routes import chat as chat_routes

    @asynccontextmanager
    async def fake_gemini_client(_settings):
        yield SimpleNamespace(models=GeminiModels())

    from app.services import gemini_provider

    monkeypatch.setattr(gemini_provider, "_gemini_client", fake_gemini_client)
    monkeypatch.setattr(
        chat_routes,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            ai_provider="gemini",
            gemini_api_key="test-key-not-real",
        ),
    )
    token_received = threading.Event()
    cancel_received = threading.Event()
    request_id = str(uuid4())

    try:
        with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
            registration = client_a.post(
                "/api/v1/auth/register",
                json={
                    "email": f"redis-owner-{uuid4().hex}@example.com",
                    "password": "correct horse battery staple",
                    "workspace_name": "Redis test workspace",
                },
            )
            assert registration.status_code == 201
            owner = registration.json()
            owner_login = client_a.post(
                "/api/v1/auth/login",
                json={
                    "email": owner["user"]["email"],
                    "password": "correct horse battery staple",
                },
            )
            assert owner_login.status_code == 200
            owner_auth = {"Authorization": f"Bearer {owner_login.json()['access_token']}"}
            workspace_id = owner["workspaces"][0]["id"]
            outsider_registration = client_b.post(
                "/api/v1/auth/register",
                json={
                    "email": f"redis-outsider-{uuid4().hex}@example.com",
                    "password": "correct horse battery staple",
                    "workspace_name": "Other workspace",
                },
            )
            assert outsider_registration.status_code == 201
            outsider_login = client_b.post(
                "/api/v1/auth/login",
                json={
                    "email": outsider_registration.json()["user"]["email"],
                    "password": "correct horse battery staple",
                },
            )
            assert outsider_login.status_code == 200
            outsider_auth = {
                "Authorization": f"Bearer {outsider_login.json()['access_token']}"
            }

            def read_generation() -> None:
                try:
                    with client_a.stream(
                        "POST",
                        "/api/v1/chat/stream",
                        headers=owner_auth,
                        json={
                            "message": "Do not store this prompt in Redis",
                            "workspace_id": workspace_id,
                            "request_id": request_id,
                        },
                    ) as response:
                        assert response.status_code == 200
                        pending = ""
                        for chunk in response.iter_text():
                            pending += chunk
                            if "event: token" in pending:
                                token_received.set()
                            if "event: cancelled" in pending:
                                cancel_received.set()
                                return
                except Exception as exc:
                    if not cancel_received.is_set():
                        raise exc

            worker = threading.Thread(target=read_generation, daemon=True)
            worker.start()
            assert token_received.wait(timeout=10)
            assert provider_waiting.wait(timeout=10)

            unauthorized = client_b.post(
                f"/api/v1/chat/stream/{request_id}/cancel",
                headers=outsider_auth,
            )
            assert unauthorized.status_code == 404
            with factory() as db:
                membership = db.scalar(
                    select(WorkspaceMembership).where(
                        WorkspaceMembership.user_id == UUID(owner["user"]["id"]),
                        WorkspaceMembership.workspace_id == UUID(workspace_id),
                    )
                )
                assert membership is not None
                db.delete(membership)
                db.commit()
            wrong_workspace = client_b.post(
                f"/api/v1/chat/stream/{request_id}/cancel",
                headers=owner_auth,
            )
            assert wrong_workspace.status_code in {403, 404}
            with factory() as db:
                db.add(
                    WorkspaceMembership(
                        user_id=UUID(owner["user"]["id"]),
                        workspace_id=UUID(workspace_id),
                        role="owner",
                    )
                )
                db.commit()

            first_stop = client_b.post(
                f"/api/v1/chat/stream/{request_id}/cancel",
                headers=owner_auth,
            )
            repeated_stop = client_b.post(
                f"/api/v1/chat/stream/{request_id}/cancel",
                headers=owner_auth,
            )
            assert first_stop.status_code == repeated_stop.status_code == 202
            worker.join(timeout=10)
            assert not worker.is_alive()
            assert cancel_received.is_set()
            assert provider_closed.is_set()

            with factory() as db:
                generation = db.scalar(
                    select(ChatGeneration).where(
                        ChatGeneration.request_id == UUID(request_id)
                    )
                )
                messages = list(
                    db.scalars(select(Message).order_by(Message.sequence))
                )
                assert generation is not None
                assert generation.status == "cancelled"
                assert [(message.role, message.content, message.status) for message in messages] == [
                    ("user", "Do not store this prompt in Redis", "completed"),
                    ("assistant", "partial output", "interrupted"),
                ]
    finally:
        engine.dispose()
