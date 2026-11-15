import pytest
from fastapi.testclient import TestClient

from app.main import app, fastapi_app


@pytest.mark.parametrize(
    "origin",
    ["http://localhost:5173", "http://127.0.0.1:5173"],
)
@pytest.mark.parametrize(
    "requested_method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
)
def test_cors_preflight_allows_development_origins_and_methods(
    client, origin, requested_method
):
    response = client.options(
        "/api/v1/auth/register",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": requested_method,
            "Access-Control-Request-Headers": "authorization,content-type,x-request-id",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "authorization" in response.headers["access-control-allow-headers"].lower()
    assert "content-type" in response.headers["access-control-allow-headers"].lower()
    assert "x-request-id" in response.headers["access-control-allow-headers"].lower()
    assert requested_method in response.headers["access-control-allow-methods"]


def test_cors_preflight_rejects_unapproved_origin(client):
    response = client.options(
        "/api/v1/auth/register",
        headers={
            "Origin": "https://unapproved.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "origin",
    ["http://localhost:5173", "http://127.0.0.1:5173"],
)
def test_cors_headers_are_present_on_authentication_errors(client, origin):
    response = client.post(
        "/api/v1/auth/register",
        headers={"Origin": origin},
        json={},
    )

    assert response.status_code == 422
    assert response.headers["access-control-allow-origin"] == origin


def test_cors_headers_are_present_on_unhandled_api_errors():
    path = "/_test/cors-unhandled-error"

    async def fail():
        raise RuntimeError("test-only server error")

    fastapi_app.add_api_route(path, fail, methods=["GET"])
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            response = test_client.get(
                path,
                headers={"Origin": "http://localhost:5173"},
            )
    finally:
        fastapi_app.router.routes = [
            route
            for route in fastapi_app.router.routes
            if getattr(route, "path", None) != path
        ]

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
