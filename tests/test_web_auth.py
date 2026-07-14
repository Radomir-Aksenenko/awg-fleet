from __future__ import annotations

from fastapi.testclient import TestClient

from awgfleet.web import _new_session, _panel_auth, _session_user, app


def test_panel_auth_requires_every_secret(monkeypatch):
    monkeypatch.delenv("PANEL_USERNAME", raising=False)
    monkeypatch.delenv("PANEL_PASSWORD", raising=False)
    monkeypatch.delenv("PANEL_SESSION_SECRET", raising=False)
    assert _panel_auth() is None


def test_panel_session_is_signed_and_bound_to_configured_user(monkeypatch):
    monkeypatch.setenv("PANEL_USERNAME", "admin")
    monkeypatch.setenv("PANEL_PASSWORD", "password")
    monkeypatch.setenv("PANEL_SESSION_SECRET", "x" * 32)
    auth = _panel_auth()
    assert auth is not None

    token = _new_session("admin", auth[2])
    assert _session_user(token, auth) == "admin"
    assert _session_user(token + "x", auth) is None


def test_panel_session_is_invalid_after_secret_rotation(monkeypatch):
    monkeypatch.setenv("PANEL_USERNAME", "admin")
    monkeypatch.setenv("PANEL_PASSWORD", "password")
    monkeypatch.setenv("PANEL_SESSION_SECRET", "x" * 32)
    auth = _panel_auth()
    assert auth is not None
    token = _new_session("admin", auth[2])

    monkeypatch.setenv("PANEL_SESSION_SECRET", "y" * 32)
    assert _session_user(token, _panel_auth()) is None


def test_login_flow_protects_api_and_sets_secure_session(monkeypatch):
    monkeypatch.setenv("PANEL_USERNAME", "admin")
    monkeypatch.setenv("PANEL_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("PANEL_SESSION_SECRET", "x" * 32)
    with TestClient(app, base_url="https://panel.example.test") as client:
        assert client.get("/api/session").status_code == 401
        assert client.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401

        login = client.post(
            "/api/login",
            json={"username": "admin", "password": "correct horse battery staple"},
        )
        assert login.status_code == 200
        assert "Secure" in login.headers["set-cookie"]
        assert client.get("/api/session").json() == {"authenticated": True, "username": "admin"}

        assert client.post("/api/logout").status_code == 200
        assert client.get("/api/session").status_code == 401
