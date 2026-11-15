import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.models.chat_generation import ChatGeneration
from app.models.conversation import Conversation, Message
from app.models.user import User
from app.schemas.conversation import StreamChatRequest
from app.services.model_provider import ModelUnavailableError, OllamaProvider
from app.services.generation_coordination import LocalGenerationCoordinator


def register(client, email):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct horse battery staple",
            "workspace_name": f"{email} workspace",
        },
    )
    assert response.status_code == 201
    result = response.json()
    authenticated = client.post(
        "/api/v1/auth/login",
        json={
            "email": email,
            "password": "correct horse battery staple",
        },
    )
    assert authenticated.status_code == 200
    return authenticated.json()["access_token"], result["workspaces"][0]["id"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_stream_sends_actual_provider_tokens_and_persists_complete_response(
    client, monkeypatch
):
    token, workspace_id = register(client, "stream@example.com")
    provider_calls = []

    class TestProvider:
        async def stream(self, message, history=None, rag_context=None):
            provider_calls.append(message)
            yield "A streamed "
            yield "response."

    from app.api.routes import chat as chat_routes

    monkeypatch.setattr(chat_routes, "create_ai_provider", lambda _settings: TestProvider())
    response = client.post(
        "/api/v1/chat/stream",
        headers=auth(token),
        json={
            "message": "Stream this answer",
            "workspace_id": workspace_id,
            "request_id": "bc3dfebb-5dc3-467f-87fd-01cd6b513aae",
        },
    )
    events = response.text
    completion = next(
        line.removeprefix("data: ")
        for line in events.splitlines()
        if line.startswith("data: ") and '"conversation_id"' in line
    )
    complete_payload = json.loads(completion)
    conversation_id = complete_payload["conversation_id"]
    messages = client.get(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=auth(token),
    ).json()

    assert response.status_code == 200
    assert "event: started" in events
    assert events.count("event: token") == 2
    assert '"token":"A streamed "' in events
    assert '"token":"response."' in events
    assert "event: complete" in events
    assert complete_payload["reply"] == "A streamed response."
    assert provider_calls == ["Stream this answer"]
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "Stream this answer"),
        ("assistant", "A streamed response."),
    ]


def test_stream_replays_completed_request_without_duplicate_messages(client, monkeypatch):
    token, workspace_id = register(client, "stream-replay@example.com")
    calls = []

    class TestProvider:
        async def stream(self, message, history=None, rag_context=None):
            calls.append(message)
            yield "Saved once."

    from app.api.routes import chat as chat_routes

    monkeypatch.setattr(chat_routes, "create_ai_provider", lambda _settings: TestProvider())
    request = {
        "message": "Only once",
        "workspace_id": workspace_id,
        "request_id": "de6836a6-42c7-4a14-9ee5-63e518a49244",
    }

    first = client.post("/api/v1/chat/stream", headers=auth(token), json=request)
    replay = client.post("/api/v1/chat/stream", headers=auth(token), json=request)
    conversation_id = json.loads(next(
        line.removeprefix("data: ")
        for line in replay.text.splitlines()
        if line.startswith("data: ") and '"conversation_id"' in line
    ))["conversation_id"]
    messages = client.get(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=auth(token),
    ).json()

    assert first.status_code == replay.status_code == 200
    assert '"replayed":true' in replay.text
    assert calls == ["Only once"]
    assert len(messages) == 2


def test_stream_failure_after_partial_tokens_persists_nothing(client, monkeypatch):
    token, workspace_id = register(client, "stream-failure@example.com")

    class TestProvider:
        async def stream(self, message, history=None, rag_context=None):
            yield "Not complete"
            raise ModelUnavailableError

    from app.api.routes import chat as chat_routes

    monkeypatch.setattr(chat_routes, "create_ai_provider", lambda _settings: TestProvider())
    response = client.post(
        "/api/v1/chat/stream",
        headers=auth(token),
        json={
            "message": "Fail after a token",
            "workspace_id": workspace_id,
            "request_id": "484f6c43-9da7-4da0-8d61-234ec1e224ae",
        },
    )

    conversations = client.get(
        f"/api/v1/conversations?workspace_id={workspace_id}",
        headers=auth(token),
    )
    assert response.status_code == 200
    assert '"token":"Not complete"' in response.text
    assert "event: error" in response.text
    assert conversations.json() == []


def test_stream_rejects_cross_workspace_and_unauthenticated_requests(
    client, monkeypatch
):
    _, owner_workspace = register(client, "stream-owner@example.com")
    outsider_token, _ = register(client, "stream-outsider@example.com")
    called = False

    class TestProvider:
        async def stream(self, message, history=None, rag_context=None):
            nonlocal called
            called = True
            yield "Must not run."

    from app.api.routes import chat as chat_routes

    monkeypatch.setattr(chat_routes, "create_ai_provider", lambda _settings: TestProvider())
    body = {
        "message": "Unauthorized",
        "workspace_id": owner_workspace,
        "request_id": "ad958c5c-c62d-42fc-a5aa-16b02bfc8a0f",
    }

    denied = client.post("/api/v1/chat/stream", headers=auth(outsider_token), json=body)
    missing_auth = client.post("/api/v1/chat/stream", json=body)

    assert denied.status_code == 404
    assert missing_auth.status_code == 401
    assert called is False


def test_stream_reservation_rejects_reuse_with_different_message(client, monkeypatch):
    token, workspace_id = register(client, "stream-conflict@example.com")

    class TestProvider:
        async def stream(self, message, history=None, rag_context=None):
            yield "Complete."

    from app.api.routes import chat as chat_routes

    monkeypatch.setattr(chat_routes, "create_ai_provider", lambda _settings: TestProvider())
    request_id = "674c3c4e-e724-4375-a2a9-72c2fb291205"
    first = client.post(
        "/api/v1/chat/stream",
        headers=auth(token),
        json={"message": "First", "workspace_id": workspace_id, "request_id": request_id},
    )
    conflict = client.post(
        "/api/v1/chat/stream",
        headers=auth(token),
        json={"message": "Different", "workspace_id": workspace_id, "request_id": request_id},
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert "already used" in conflict.json()["detail"]


def test_stale_stream_reservation_can_be_retried(
    client, test_session_factory, monkeypatch
):
    token, workspace_id = register(client, "stream-stale@example.com")
    request_id = "180d10a1-c4ea-42d2-b2a5-8910d449e72f"
    request = {
        "message": "Retry after server restart",
        "workspace_id": workspace_id,
        "request_id": request_id,
    }

    class TestProvider:
        async def stream(self, message, history=None, rag_context=None):
            yield "Recovered response."

    from app.api.routes import chat as chat_routes

    monkeypatch.setattr(chat_routes, "create_ai_provider", lambda _settings: TestProvider())
    with test_session_factory() as db:
        user = db.scalar(select(User).where(User.email == "stream-stale@example.com"))
        assert user is not None
        stale_time = datetime.now(timezone.utc) - timedelta(minutes=30)
        db.add(
            ChatGeneration(
                user_id=user.id,
                request_id=UUID(request_id),
                request_fingerprint=chat_routes._request_fingerprint(
                    StreamChatRequest(**request)
                ),
                status="processing",
                created_at=stale_time,
                updated_at=stale_time,
                grounded=False,
                sources=[],
            )
        )
        db.commit()

    response = client.post("/api/v1/chat/stream", headers=auth(token), json=request)
    with test_session_factory() as db:
        generations = list(db.scalars(select(ChatGeneration)))

    assert response.status_code == 200
    assert len(generations) == 1
    assert generations[0].status == "completed"
    assert generations[0].reply == "Recovered response."


def test_closing_provider_stream_closes_ollama_response(monkeypatch):
    from app.core.config import Settings
    from app.services import model_provider

    closed = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            yield '{"message":{"content":"First token"},"done":false}'
            await asyncio.Event().wait()

    class ResponseContext:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *_args):
            closed.append("response")

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            closed.append("client")

        def stream(self, *_args, **_kwargs):
            return ResponseContext()

    monkeypatch.setattr(model_provider.httpx, "AsyncClient", FakeClient)

    async def verify_closure():
        stream = OllamaProvider(Settings()).stream("Question")
        assert await anext(stream) == "First token"
        await stream.aclose()

    asyncio.run(verify_closure())
    assert closed == ["response", "client"]


def test_closing_sse_consumer_persists_partial_as_interrupted(
    client, test_session_factory, monkeypatch
):
    token, _workspace_id = register(client, "stream-cancel@example.com")
    provider_closed = []

    class TestProvider:
        async def stream(self, message, history=None, rag_context=None):
            try:
                yield "Partial token"
                await asyncio.Event().wait()
            finally:
                provider_closed.append(True)

    from app.api.routes import chat as chat_routes

    monkeypatch.setattr(chat_routes, "create_ai_provider", lambda _settings: TestProvider())
    coordinator = LocalGenerationCoordinator()
    with test_session_factory() as db:
        user = db.scalar(select(User).where(User.email == "stream-cancel@example.com"))
        assert user is not None
        body = StreamChatRequest(
            message="Cancel this stream",
            workspace_id=_workspace_id,
            request_id="3ec92e56-afb3-4f41-a9bd-84bfdf2c50a3",
        )

        async def consume_then_close():
            response = await chat_routes.stream_chat(body, user, db, coordinator)
            iterator = response.body_iterator
            started_event = await anext(iterator)
            first_event = await anext(iterator)
            await iterator.aclose()
            return started_event, first_event

        started_event, first_event = asyncio.run(consume_then_close())
        assert "event: started" in started_event
        assert "event: token" in first_event
        assert provider_closed == [True]
        conversation = db.scalar(select(Conversation))
        assert conversation is not None
        messages = list(
            db.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.sequence)
            )
        )
        generation = db.scalar(select(ChatGeneration))
        assert [(message.role, message.content, message.status) for message in messages] == [
            ("user", "Cancel this stream", "completed"),
            ("assistant", "Partial token", "interrupted"),
        ]
        assert generation is not None
        assert generation.status == "cancelled"


def test_stop_endpoint_cancels_provider_stream_and_is_idempotent(
    test_session_factory, monkeypatch
):
    provider_closed = []

    class TestProvider:
        async def stream(self, message, history=None, rag_context=None):
            try:
                yield "Visible partial"
                await asyncio.Event().wait()
            finally:
                provider_closed.append(True)

    from app.api.routes import chat as chat_routes

    monkeypatch.setattr(chat_routes, "create_ai_provider", lambda _settings: TestProvider())
    coordinator = LocalGenerationCoordinator()
    with test_session_factory() as db:
        user = User(email="cancel@example.com", password_hash="unused")
        db.add(user)
        db.flush()
        from app.models.workspace import Workspace, WorkspaceMembership

        workspace = Workspace(name="Cancel workspace")
        db.add(workspace)
        db.flush()
        db.add(WorkspaceMembership(user_id=user.id, workspace_id=workspace.id, role="owner"))
        db.commit()
        body = StreamChatRequest(
            message="Stop this stream",
            workspace_id=workspace.id,
            request_id="4ec92e56-afb3-4f41-a9bd-84bfdf2c50a3",
        )

        async def consume_then_stop():
            response = await chat_routes.stream_chat(body, user, db, coordinator)
            iterator = response.body_iterator
            started_event = await anext(iterator)
            token_event = await anext(iterator)
            first_stop = await chat_routes.cancel_stream(
                body.request_id, user, db, coordinator
            )
            repeated_stop = await chat_routes.cancel_stream(
                body.request_id, user, db, coordinator
            )
            cancelled_event = await anext(iterator)
            with pytest.raises(StopAsyncIteration):
                await anext(iterator)
            return started_event, token_event, first_stop, repeated_stop, cancelled_event

        started_event, token_event, first_stop, repeated_stop, cancelled_event = asyncio.run(
            consume_then_stop()
        )
        assert "event: started" in started_event
        assert "event: token" in token_event
        assert first_stop == repeated_stop == {"status": "cancellation_requested"}
        assert "event: cancelled" in cancelled_event
        assert "event: complete" not in cancelled_event
        assert provider_closed == [True]
        assert db.scalar(select(ChatGeneration)).status == "cancelled"
        assistant = db.scalar(select(Message).where(Message.role == "assistant"))
        assert assistant is not None
        assert assistant.status == "interrupted"
        assert assistant.content == "Visible partial"


def test_stop_before_first_token_and_retry_reuses_persisted_messages(
    test_session_factory, monkeypatch
):
    calls = []

    class TestProvider:
        async def stream(self, message, history=None, rag_context=None):
            calls.append(message)
            if len(calls) == 1:
                yield "Partial retry"
                await asyncio.Event().wait()
            else:
                yield "Completed answer."

    from app.api.routes import chat as chat_routes
    from app.models.workspace import Workspace, WorkspaceMembership

    monkeypatch.setattr(chat_routes, "create_ai_provider", lambda _settings: TestProvider())
    coordinator = LocalGenerationCoordinator()
    with test_session_factory() as db:
        user = User(email="retry-cancel@example.com", password_hash="unused")
        db.add(user)
        db.flush()
        workspace = Workspace(name="Retry workspace")
        db.add(workspace)
        db.flush()
        db.add(WorkspaceMembership(user_id=user.id, workspace_id=workspace.id, role="owner"))
        db.commit()
        body = StreamChatRequest(
            message="Retry this",
            workspace_id=workspace.id,
            request_id="5ec92e56-afb3-4f41-a9bd-84bfdf2c50a3",
        )

        async def cancel_before_streaming():
            response = await chat_routes.stream_chat(body, user, db, coordinator)
            await chat_routes.cancel_stream(
                body.request_id, user, db, coordinator
            )
            events = [event async for event in response.body_iterator]
            return events

        first_events = asyncio.run(cancel_before_streaming())
        assert any("event: cancelled" in event for event in first_events)
        assert calls == []
        interrupted = list(db.scalars(select(Message).order_by(Message.sequence)))
        assert len(interrupted) == 2
        assert interrupted[1].status == "interrupted"
        assert interrupted[1].content == ""

        async def retry_and_cancel():
            response = await chat_routes.stream_chat(body, user, db, coordinator)
            iterator = response.body_iterator
            await anext(iterator)
            token_event = await anext(iterator)
            await chat_routes.cancel_stream(
                body.request_id, user, db, coordinator
            )
            cancelled_event = await anext(iterator)
            with pytest.raises(StopAsyncIteration):
                await anext(iterator)
            return token_event, cancelled_event

        retry_token, retry_cancelled = asyncio.run(retry_and_cancel())
        assert "event: token" in retry_token
        assert "event: cancelled" in retry_cancelled
        assert calls == ["Retry this"]
        messages = list(db.scalars(select(Message).order_by(Message.sequence)))
        assert len(messages) == 2
        assert messages[1].content == "Partial retry"
        assert messages[1].status == "interrupted"

        async def finish_retry():
            response = await chat_routes.stream_chat(body, user, db, coordinator)
            return [event async for event in response.body_iterator]

        final_events = asyncio.run(finish_retry())
        assert any("event: complete" in event for event in final_events)
        messages = list(db.scalars(select(Message).order_by(Message.sequence)))
        assert len(messages) == 2
        assert messages[1].content == "Completed answer."
        assert messages[1].status == "completed"


def test_stop_during_rag_preparation_cancels_embedding_work(
    test_session_factory, monkeypatch
):
    from app.api.routes import chat as chat_routes
    from app.models.workspace import Workspace, WorkspaceMembership

    preparation_started = asyncio.Event()
    preparation_cancelled = []

    async def slow_rag_preparation(_prepared, _db, _request_id=None):
        preparation_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            preparation_cancelled.append(True)
            raise

    monkeypatch.setattr(chat_routes, "_prepare_rag", slow_rag_preparation)
    coordinator = LocalGenerationCoordinator()
    with test_session_factory() as db:
        user = User(email="rag-cancel@example.com", password_hash="unused")
        db.add(user)
        db.flush()
        workspace = Workspace(name="RAG cancel workspace")
        db.add(workspace)
        db.flush()
        db.add(WorkspaceMembership(
            user_id=user.id, workspace_id=workspace.id, role="owner"
        ))
        db.commit()
        body = StreamChatRequest(
            message="Stop during retrieval",
            workspace_id=workspace.id,
            request_id="6ec92e56-afb3-4f41-a9bd-84bfdf2c50a3",
        )

        async def start_and_stop():
            response = await chat_routes.stream_chat(body, user, db, coordinator)
            iterator = response.body_iterator
            started_event = await anext(iterator)
            assert not preparation_started.is_set()
            next_event = asyncio.create_task(anext(iterator))
            await preparation_started.wait()
            await chat_routes.cancel_stream(
                body.request_id, user, db, coordinator
            )
            cancelled_event = await next_event
            with pytest.raises(StopAsyncIteration):
                await anext(iterator)
            return started_event, cancelled_event

        started_event, cancelled_event = asyncio.run(start_and_stop())
        assert "event: started" in started_event
        assert "event: cancelled" in cancelled_event
        assert preparation_cancelled == [True]
        generation = db.scalar(select(ChatGeneration))
        assert generation is not None
        assert generation.status == "cancelled"
        assistant = db.scalar(select(Message).where(Message.role == "assistant"))
        assert assistant is not None
        assert assistant.content == ""
        assert assistant.status == "interrupted"


def test_disconnect_during_rag_preparation_cleans_up_embedding_task(
    test_session_factory, monkeypatch
):
    from app.api.routes import chat as chat_routes
    from app.models.workspace import Workspace, WorkspaceMembership

    preparation_started = asyncio.Event()
    preparation_cancelled = []

    async def slow_rag_preparation(_prepared, _db, _request_id=None):
        preparation_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            preparation_cancelled.append(True)
            raise

    monkeypatch.setattr(chat_routes, "_prepare_rag", slow_rag_preparation)
    coordinator = LocalGenerationCoordinator()
    with test_session_factory() as db:
        user = User(email="rag-disconnect@example.com", password_hash="unused")
        db.add(user)
        db.flush()
        workspace = Workspace(name="RAG disconnect workspace")
        db.add(workspace)
        db.flush()
        db.add(WorkspaceMembership(
            user_id=user.id, workspace_id=workspace.id, role="owner"
        ))
        db.commit()
        body = StreamChatRequest(
            message="Disconnect during retrieval",
            workspace_id=workspace.id,
            request_id="7ec92e56-afb3-4f41-a9bd-84bfdf2c50a3",
        )

        async def disconnect():
            response = await chat_routes.stream_chat(body, user, db, coordinator)
            iterator = response.body_iterator
            assert "event: started" in await anext(iterator)
            next_event = asyncio.create_task(anext(iterator))
            await preparation_started.wait()
            next_event.cancel()
            with pytest.raises(asyncio.CancelledError):
                await next_event

        asyncio.run(disconnect())
        assert preparation_cancelled == [True]
        generation = db.scalar(select(ChatGeneration))
        assert generation is not None
        assert generation.status == "cancelled"
        assistant = db.scalar(select(Message).where(Message.role == "assistant"))
        assert assistant is not None
        assert assistant.content == ""
        assert assistant.status == "interrupted"


def test_cancel_endpoint_requires_the_generation_owner_and_valid_auth(client, monkeypatch):
    owner_token, workspace_id = register(client, "cancel-owner@example.com")
    outsider_token, _ = register(client, "cancel-outsider@example.com")

    class TestProvider:
        async def stream(self, message, history=None, rag_context=None):
            yield "Complete."

    from app.api.routes import chat as chat_routes

    monkeypatch.setattr(chat_routes, "create_ai_provider", lambda _settings: TestProvider())
    request_id = "6ec92e56-afb3-4f41-a9bd-84bfdf2c50a3"
    response = client.post(
        "/api/v1/chat/stream",
        headers=auth(owner_token),
        json={
            "message": "Finish before cancel",
            "workspace_id": workspace_id,
            "request_id": request_id,
        },
    )
    outsider_cancel = client.post(
        f"/api/v1/chat/stream/{request_id}/cancel",
        headers=auth(outsider_token),
    )
    unauthenticated_cancel = client.post(
        f"/api/v1/chat/stream/{request_id}/cancel"
    )
    completed_cancel = client.post(
        f"/api/v1/chat/stream/{request_id}/cancel",
        headers=auth(owner_token),
    )

    assert response.status_code == 200
    assert "event: complete" in response.text
    assert outsider_cancel.status_code == 404
    assert unauthenticated_cancel.status_code == 401
    assert completed_cancel.status_code == 409


def test_ollama_provider_stream_parses_tokens_and_requires_done(monkeypatch):
    from app.core.config import Settings
    from app.services import model_provider

    class FakeResponse:
        def __init__(self, lines):
            self.lines = lines

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            for line in self.lines:
                yield line

    class ResponseContext:
        def __init__(self, lines):
            self.response = FakeResponse(lines)

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, *_args):
            pass

    class FakeClient:
        def __init__(self, lines, **_kwargs):
            self.lines = lines

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def stream(self, *_args, **_kwargs):
            return ResponseContext(self.lines)

    async def collect(provider):
        return [token async for token in provider.stream("Question")]

    monkeypatch.setattr(
        model_provider.httpx,
        "AsyncClient",
        lambda **kwargs: FakeClient(
            [
                '{"message":{"content":"Hello "},"done":false}',
                '{"message":{"content":"world"},"done":false}',
                '{"done":true}',
            ],
            **kwargs,
        ),
    )
    assert asyncio.run(collect(OllamaProvider(Settings()))) == ["Hello ", "world"]

    monkeypatch.setattr(
        model_provider.httpx,
        "AsyncClient",
        lambda **kwargs: FakeClient(
            ['{"message":{"content":"partial"},"done":false}'],
            **kwargs,
        ),
    )
    with pytest.raises(ModelUnavailableError):
        asyncio.run(collect(OllamaProvider(Settings())))


def test_ollama_timeouts_allow_slow_model_load_without_long_connect_wait():
    from app.core.config import Settings
    from app.services.model_provider import ollama_timeout

    timeout = ollama_timeout(Settings())

    assert timeout.connect == 10
    assert timeout.read == 600
    assert timeout.write == 90
