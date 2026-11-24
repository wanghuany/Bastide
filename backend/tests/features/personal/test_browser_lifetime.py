"""Browser presence is independent of authentication and never writes financial data."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.features.personal.browser_lifetime import BrowserLifetime
from app.personal import create_personal_app
from tests.features.personal.test_personal import PASSWORD, accounts, create_entry, report


def test_grace_refresh_multiple_tabs_and_startup_timeout():
    now = [0.0]
    lifetime = BrowserLifetime(lambda: None, clock=lambda: now[0])
    now[0] = 119
    assert not lifetime.shutdown_due()
    first = lifetime.connect()
    second = lifetime.connect()
    lifetime.disconnect(first)
    now[0] = 200
    assert not lifetime.shutdown_due()  # Another page remains, including background tabs.
    lifetime.disconnect(second)
    now[0] = 204
    assert not lifetime.shutdown_due()
    refreshed = lifetime.connect()
    now[0] = 300
    assert not lifetime.shutdown_due()
    lifetime.disconnect(refreshed)
    now[0] = 304
    lifetime.disconnect(refreshed)  # Duplicate disconnect must not reset the timer.
    assert not lifetime.shutdown_due()
    now[0] = 305
    assert lifetime.shutdown_due()


def test_no_page_opens_startup_timeout():
    now = [0.0]
    lifetime = BrowserLifetime(lambda: None, clock=lambda: now[0])
    now[0] = 120
    assert lifetime.shutdown_due()


def test_monitor_requests_graceful_exit_once():
    exits = []
    lifetime = BrowserLifetime(lambda: exits.append(True), startup_seconds=0)
    asyncio.run(lifetime.watch())
    assert lifetime.stopping and exits == [True]


def test_page_presence_locked_unlocked_and_clean_vault_shutdown(tmp_path):
    lifetime = BrowserLifetime(lambda: None)
    path = tmp_path / "fictional.bfin"
    app = create_personal_app(path, browser_lifetime=lifetime)
    with TestClient(app, base_url="http://127.0.0.1:8899") as client:
        assert client.get("/api/window").json() == {"exit_on_close": True}
        with client.websocket_connect(
            "/api/window/ws",
            headers={"host": "127.0.0.1:8899", "origin": "http://127.0.0.1:8899"},
        ):
            assert len(lifetime.connections) == 1
            client.post("/api/setup", json={"password": PASSWORD}).raise_for_status()
            create_entry(client)
            assert report(client)["expense_minor"] == 1250
            with client.websocket_connect(
                "/api/window/ws",
                headers={"host": "127.0.0.1:8899", "origin": "http://127.0.0.1:8899"},
            ):
                assert len(lifetime.connections) == 2
                client.post("/api/lock").raise_for_status()
                assert len(lifetime.connections) == 2
            assert len(lifetime.connections) == 1
            client.post("/api/unlock", json={"credential": PASSWORD}).raise_for_status()
        assert not lifetime.connections
        assert lifetime.empty_since is not None
    assert not app.state.personal.vault.unlocked
    # Same vault can be reopened: clean shutdown released the file lock and preserved writes.
    with TestClient(create_personal_app(path), base_url="http://127.0.0.1:8899") as client:
        client.post("/api/unlock", json={"credential": PASSWORD}).raise_for_status()
        assert accounts(client)
        assert report(client)["expense_minor"] == 1250


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("127.0.0.1:8899", "https://evil.example"),
        ("evil.example", "http://evil.example"),
        ("127.0.0.1:8899", None),
        ("127.0.0.1:8899", "http://localhost:8899"),
    ],
)
def test_cross_origin_sockets_cannot_keep_process_alive(tmp_path, host, origin):
    lifetime = BrowserLifetime(lambda: None)
    with TestClient(
        create_personal_app(tmp_path / "fictional.bfin", browser_lifetime=lifetime)
    ) as c:
        headers = {"host": host}
        if origin is not None:
            headers["origin"] = origin
        with (
            pytest.raises(WebSocketDisconnect) as closed,
            c.websocket_connect("/api/window/ws", headers=headers),
        ):
            pytest.fail("Invalid origin was accepted")
        assert closed.value.code == 1008
        assert not lifetime.connections


def test_background_mode_does_not_enable_close_monitor(tmp_path):
    with TestClient(
        create_personal_app(tmp_path / "fictional.bfin"), base_url="http://127.0.0.1:8899"
    ) as client:
        assert client.get("/api/window").json() == {"exit_on_close": False}
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/api/window/ws", headers={"origin": "http://testserver"}),
        ):
            pytest.fail("Background mode should not open a browser connection")
