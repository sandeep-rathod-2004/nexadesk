from app.core.config import Settings


def test_model_info_reports_configured_ollama_model(client, monkeypatch):
    token_response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "model-info@example.com",
            "password": "correct horse battery staple",
            "workspace_name": "Model info",
        },
    )
    assert token_response.status_code == 201
    authenticated = client.post(
        "/api/v1/auth/login",
        json={
            "email": "model-info@example.com",
            "password": "correct horse battery staple",
        },
    )
    assert authenticated.status_code == 200
    token = authenticated.json()["access_token"]

    from app.api.routes import chat as chat_routes

    monkeypatch.setattr(
        chat_routes,
        "get_settings",
        lambda: Settings(ollama_model="phi3:mini"),
    )
    response = client.get(
        "/api/v1/model",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"provider": "Ollama", "model": "phi3:mini"}


def test_model_info_requires_authentication(client):
    response = client.get("/api/v1/model")

    assert response.status_code == 401
