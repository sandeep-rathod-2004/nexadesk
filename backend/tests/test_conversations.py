from uuid import UUID, uuid4

from sqlalchemy import select

from app.services.model_provider import OllamaProvider
from app.models.knowledge import KnowledgeDocument
from app.models.user import User


def register(client, email, workspace_name):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct horse battery staple",
            "workspace_name": workspace_name,
        },
    )
    assert response.status_code == 201
    payload = response.json()
    authenticated = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert authenticated.status_code == 200
    return authenticated.json()["access_token"], payload["workspaces"][0]["id"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_conversation_create_list_and_message_retrieval(client):
    token, workspace_id = register(client, "creator@example.com", "Creator workspace")
    created = client.post(
        "/api/v1/conversations",
        headers=auth(token),
        json={"workspace_id": workspace_id, "title": "Project discussion"},
    )

    assert created.status_code == 201
    conversation_id = created.json()["id"]
    listed = client.get(
        f"/api/v1/conversations?workspace_id={workspace_id}",
        headers=auth(token),
    )
    messages = client.get(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=auth(token),
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [conversation_id]
    assert messages.status_code == 200
    assert messages.json() == []


def test_conversation_rename_and_delete(client):
    token, workspace_id = register(client, "manage@example.com", "Management")
    created = client.post(
        "/api/v1/conversations",
        headers=auth(token),
        json={"workspace_id": workspace_id, "title": "Original title"},
    )
    conversation_id = created.json()["id"]

    renamed = client.patch(
        f"/api/v1/conversations/{conversation_id}",
        headers=auth(token),
        json={"title": "  Updated title  "},
    )
    deleted = client.delete(
        f"/api/v1/conversations/{conversation_id}",
        headers=auth(token),
    )
    missing = client.get(
        f"/api/v1/conversations/{conversation_id}",
        headers=auth(token),
    )

    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Updated title"
    assert deleted.status_code == 204
    assert missing.status_code == 404


def test_only_conversation_owner_can_rename_or_delete(client):
    owner_token, workspace_id = register(client, "owner@example.com", "Shared space")
    member_token, _ = register(client, "member@example.com", "Member space")
    created = client.post(
        "/api/v1/conversations",
        headers=auth(owner_token),
        json={"workspace_id": workspace_id, "title": "Owner's conversation"},
    )
    conversation_id = created.json()["id"]
    assert client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=auth(owner_token),
        json={"email": "member@example.com"},
    ).status_code == 201

    rename_denied = client.patch(
        f"/api/v1/conversations/{conversation_id}",
        headers=auth(member_token),
        json={"title": "Changed by member"},
    )
    delete_denied = client.delete(
        f"/api/v1/conversations/{conversation_id}",
        headers=auth(member_token),
    )
    still_exists = client.get(
        f"/api/v1/conversations/{conversation_id}",
        headers=auth(owner_token),
    )

    assert rename_denied.status_code == 404
    assert delete_denied.status_code == 404
    assert still_exists.status_code == 200


def test_users_cannot_access_another_workspace_or_owned_conversation(client):
    owner_token, owner_workspace = register(
        client, "owner@example.com", "Owner workspace"
    )
    stranger_token, _ = register(client, "stranger@example.com", "Stranger workspace")
    created = client.post(
        "/api/v1/conversations",
        headers=auth(owner_token),
        json={"workspace_id": owner_workspace, "title": "Private conversation"},
    )
    conversation_id = created.json()["id"]

    cross_workspace_create = client.post(
        "/api/v1/conversations",
        headers=auth(stranger_token),
        json={"workspace_id": owner_workspace, "title": "Unauthorized"},
    )
    cross_workspace_list = client.get(
        f"/api/v1/conversations?workspace_id={owner_workspace}",
        headers=auth(stranger_token),
    )
    cross_workspace_get = client.get(
        f"/api/v1/conversations/{conversation_id}",
        headers=auth(stranger_token),
    )
    cross_workspace_messages = client.get(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=auth(stranger_token),
    )

    assert cross_workspace_create.status_code == 404
    assert cross_workspace_list.status_code == 404
    assert cross_workspace_get.status_code == 404
    assert cross_workspace_messages.status_code == 404


def test_workspace_member_cannot_read_another_members_conversation(client):
    owner_token, workspace_id = register(client, "owner@example.com", "Shared space")
    member_token, _ = register(client, "member@example.com", "Member space")
    created = client.post(
        "/api/v1/conversations",
        headers=auth(owner_token),
        json={"workspace_id": workspace_id, "title": "Owner's conversation"},
    )
    conversation_id = created.json()["id"]

    added = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=auth(owner_token),
        json={"email": "member@example.com"},
    )
    denied = client.get(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=auth(member_token),
    )

    assert added.status_code == 201
    assert denied.status_code == 404


def test_workspace_member_cannot_add_other_members(client):
    owner_token, workspace_id = register(client, "owner@example.com", "Shared space")
    member_token, _ = register(client, "member@example.com", "Member space")
    assert client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=auth(owner_token),
        json={"email": "member@example.com"},
    ).status_code == 201

    denied = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=auth(member_token),
        json={"email": "owner@example.com"},
    )

    assert denied.status_code == 403


def test_knowledge_chat_documents_are_workspace_scoped_and_returned(
    client, test_session_factory
):
    owner_token, owner_workspace = register(
        client, "knowledge-owner@example.com", "Knowledge workspace"
    )
    _, other_workspace = register(
        client, "knowledge-other@example.com", "Other workspace"
    )
    with test_session_factory() as db:
        owner = db.scalar(
            select(User).where(User.email == "knowledge-owner@example.com")
        )
        other = db.scalar(
            select(User).where(User.email == "knowledge-other@example.com")
        )
        assert owner is not None
        assert other is not None
        owner_document = KnowledgeDocument(
            workspace_id=UUID(owner_workspace),
            uploaded_by_id=owner.id,
            filename="owner.txt",
            storage_key=f"{uuid4().hex}.txt",
            media_type="text/plain",
            size_bytes=10,
            status="ready",
        )
        owner_document_two = KnowledgeDocument(
            workspace_id=UUID(owner_workspace),
            uploaded_by_id=owner.id,
            filename="owner-update.txt",
            storage_key=f"{uuid4().hex}.txt",
            media_type="text/plain",
            size_bytes=12,
            status="ready",
        )
        other_document = KnowledgeDocument(
            workspace_id=UUID(other_workspace),
            uploaded_by_id=other.id,
            filename="other.txt",
            storage_key=f"{uuid4().hex}.txt",
            media_type="text/plain",
            size_bytes=10,
            status="ready",
        )
        db.add_all([owner_document, owner_document_two, other_document])
        db.commit()
        owner_document_id = str(owner_document.id)
        owner_document_two_id = str(owner_document_two.id)
        other_document_id = str(other_document.id)

    created = client.post(
        "/api/v1/conversations",
        headers=auth(owner_token),
        json={
            "workspace_id": owner_workspace,
            "chat_type": "knowledge",
            "document_ids": [owner_document_id],
        },
    )
    assert created.status_code == 201
    assert created.json()["chat_type"] == "knowledge"
    assert created.json()["document_ids"] == [owner_document_id]
    updated = client.put(
        f"/api/v1/conversations/{created.json()['id']}/documents",
        headers=auth(owner_token),
        json={"document_ids": [owner_document_two_id]},
    )
    assert updated.status_code == 200
    assert updated.json()["document_ids"] == [owner_document_two_id]

    cross_workspace_document = client.post(
        "/api/v1/conversations",
        headers=auth(owner_token),
        json={
            "workspace_id": owner_workspace,
            "chat_type": "knowledge",
            "document_ids": [other_document_id],
        },
    )
    normal_with_document = client.post(
        "/api/v1/conversations",
        headers=auth(owner_token),
        json={
            "workspace_id": owner_workspace,
            "chat_type": "normal",
            "document_ids": [owner_document_id],
        },
    )
    update_cross_workspace_document = client.put(
        f"/api/v1/conversations/{created.json()['id']}/documents",
        headers=auth(owner_token),
        json={"document_ids": [other_document_id]},
    )
    listed_knowledge = client.get(
        f"/api/v1/conversations?workspace_id={owner_workspace}&chat_type=knowledge",
        headers=auth(owner_token),
    )
    listed_normal = client.get(
        f"/api/v1/conversations?workspace_id={owner_workspace}&chat_type=normal",
        headers=auth(owner_token),
    )

    assert cross_workspace_document.status_code == 404
    assert normal_with_document.status_code == 422
    assert update_cross_workspace_document.status_code == 404
    assert [item["id"] for item in listed_knowledge.json()] == [created.json()["id"]]
    assert listed_knowledge.json()[0]["document_ids"] == [owner_document_two_id]
    assert listed_normal.json() == []


def test_normal_chat_skips_workspace_rag_even_when_documents_exist(
    client, test_session_factory, monkeypatch
):
    token, workspace_id = register(client, "normal-chat@example.com", "Normal workspace")
    with test_session_factory() as db:
        user = db.scalar(
            select(User).where(User.email == "normal-chat@example.com")
        )
        assert user is not None
        db.add(
            KnowledgeDocument(
                workspace_id=UUID(workspace_id),
                uploaded_by_id=user.id,
                filename="workspace-policy.txt",
                storage_key=f"{uuid4().hex}.txt",
                media_type="text/plain",
                size_bytes=10,
                status="ready",
            )
        )
        db.commit()

    from app.api.routes import chat as chat_routes

    async def unexpected_embedding(*_args, **_kwargs):
        raise AssertionError("Normal Chats must not call the embedding provider.")

    async def fake_generate(self, message, history=None, rag_context=None):
        assert rag_context is None
        return f"Direct answer: {message}"

    monkeypatch.setattr(chat_routes, "embed_texts", unexpected_embedding)
    monkeypatch.setattr(OllamaProvider, "generate", fake_generate)
    response = client.post(
        "/api/v1/chat",
        headers=auth(token),
        json={"message": "A general question", "workspace_id": workspace_id},
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "Direct answer: A general question"
    assert response.json()["grounded"] is False


def test_knowledge_chat_does_not_fall_back_when_associated_documents_are_unready(
    client, test_session_factory, monkeypatch
):
    token, workspace_id = register(client, "unready-knowledge@example.com", "Knowledge")
    with test_session_factory() as db:
        user = db.scalar(
            select(User).where(User.email == "unready-knowledge@example.com")
        )
        assert user is not None
        document = KnowledgeDocument(
            workspace_id=UUID(workspace_id),
            uploaded_by_id=user.id,
            filename="temporarily-unready.txt",
            storage_key=f"{uuid4().hex}.txt",
            media_type="text/plain",
            size_bytes=10,
            status="ready",
        )
        db.add(document)
        db.commit()
        document_id = document.id

    created = client.post(
        "/api/v1/conversations",
        headers=auth(token),
        json={
            "workspace_id": workspace_id,
            "chat_type": "knowledge",
            "document_ids": [str(document_id)],
        },
    )
    assert created.status_code == 201
    with test_session_factory() as db:
        document = db.get(KnowledgeDocument, document_id)
        assert document is not None
        document.status = "failed"
        db.commit()

    from app.api.routes import chat as chat_routes

    async def unexpected_provider(*_args, **_kwargs):
        raise AssertionError("Knowledge Chat must not answer without ready sources.")

    monkeypatch.setattr(chat_routes, "embed_texts", unexpected_provider)
    monkeypatch.setattr(OllamaProvider, "generate", unexpected_provider)
    response = client.post(
        "/api/v1/chat",
        headers=auth(token),
        json={
            "workspace_id": workspace_id,
            "conversation_id": created.json()["id"],
            "message": "Question requiring a source",
        },
    )

    assert response.status_code == 200
    assert "not currently ready" in response.json()["reply"]
    assert response.json()["sources"] == []


def test_chat_persists_both_messages_and_reuses_history(client, monkeypatch):
    token, workspace_id = register(client, "chat@example.com", "Chat workspace")
    seen_history: list[list[dict[str, str]]] = []

    async def fake_generate(self, message, history=None):
        seen_history.append(history or [])
        return f"Reply to: {message}"

    monkeypatch.setattr(OllamaProvider, "generate", fake_generate)
    first = client.post(
        "/api/v1/chat",
        headers=auth(token),
        json={"message": "First question", "workspace_id": workspace_id},
    )
    conversation_id = first.json()["conversation_id"]
    second = client.post(
        "/api/v1/chat",
        headers=auth(token),
        json={
            "message": "Follow-up",
            "workspace_id": workspace_id,
            "conversation_id": conversation_id,
        },
    )
    persisted = client.get(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=auth(token),
    )
    conversations = client.get(
        f"/api/v1/conversations?workspace_id={workspace_id}",
        headers=auth(token),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["conversation_id"] == conversation_id
    assert [message["role"] for message in persisted.json()] == [
        "user", "assistant", "user", "assistant"
    ]
    assert [message["content"] for message in persisted.json()] == [
        "First question", "Reply to: First question", "Follow-up", "Reply to: Follow-up"
    ]
    assert len(conversations.json()) == 1
    assert seen_history[0] == []
    assert seen_history[1] == [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "Reply to: First question"},
    ]


def test_chat_requires_authentication_and_does_not_persist_provider_failure(
    client, monkeypatch
):
    token, workspace_id = register(client, "failure@example.com", "Failure workspace")

    async def failed_generate(self, message, history=None):
        from app.services.model_provider import ModelUnavailableError

        raise ModelUnavailableError

    monkeypatch.setattr(OllamaProvider, "generate", failed_generate)
    unauthorized = client.post(
        "/api/v1/chat",
        json={"message": "question", "workspace_id": workspace_id},
    )
    failed = client.post(
        "/api/v1/chat",
        headers=auth(token),
        json={"message": "question", "workspace_id": workspace_id},
    )
    listed = client.get(
        f"/api/v1/conversations?workspace_id={workspace_id}",
        headers=auth(token),
    )

    assert unauthorized.status_code == 401
    assert failed.status_code == 503
    assert listed.json() == []
