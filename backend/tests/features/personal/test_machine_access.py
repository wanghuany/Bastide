"""Device authorization uses fictional passwords and isolated credential directories."""

import os
import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.errors import AuthError
from app.features.personal.machine_access import MachineAccess, protect
from app.personal import create_personal_app
from tests.features.personal.test_personal import PASSWORD

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
get_settings.cache_clear()


def test_dpapi_ciphertext_and_vault_path_binding(tmp_path):
    value = b"fictional-not-a-real-password"
    encrypted = protect(value)
    assert value not in encrypted
    assert protect(encrypted, decrypt=True) == value
    with pytest.raises(AuthError):
        protect(encrypted[:-1], decrypt=True)
    a = MachineAccess(tmp_path / "a.bfin", tmp_path / "credentials")
    b = MachineAccess(tmp_path / "b.bfin", tmp_path / "credentials")
    a.save(value.decode())
    assert a.enabled and not b.enabled
    assert a.load() == value.decode()
    assert value not in a.path.read_bytes()
    a.revoke()
    assert not a.enabled


def test_authorize_reopen_agent_access_without_web_auth_and_revoke(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    path = tmp_path / "fictional.bfin"
    with TestClient(create_personal_app(path), base_url="http://127.0.0.1:8899") as c:
        assert c.post("/api/machine-access", json={"password": PASSWORD}).status_code == 401
        c.post("/api/setup", json={"password": PASSWORD}).raise_for_status()
        token = c.post("/api/agent-token", json={"allow_writes": True}).json()["token"]
        response = c.post("/api/machine-access", json={"password": PASSWORD})
        assert response.status_code == 200
        assert c.get("/api/accounts").status_code == 200
        assert c.get("/api/machine-access").json()["active"]
    app = create_personal_app(path)
    with TestClient(app, base_url="http://127.0.0.1:8899") as c:
        assert c.get("/api/status").json()["authenticated"] is False
        assert c.get("/api/accounts").status_code == 401
        assert app.state.personal.vault.unlocked
        app.state.personal.last_activity = time.monotonic() - 10000
        assert (
            c.get("/api/agent/catalog", headers={"Authorization": "Bearer " + token}).status_code
            == 200
        )
        c.post("/api/unlock", json={"credential": PASSWORD}).raise_for_status()
        assert c.delete("/api/machine-access").status_code == 200
        assert not MachineAccess(path).enabled
        assert (
            c.get("/api/agent/catalog", headers={"Authorization": "Bearer " + token}).status_code
            == 401
        )
    with TestClient(create_personal_app(path), base_url="http://127.0.0.1:8899") as c:
        assert c.get("/api/status").json()["unlocked"] is False


def test_lock_and_password_change_revoke_device(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    path = tmp_path / "fictional.bfin"
    with TestClient(create_personal_app(path), base_url="http://127.0.0.1:8899") as c:
        c.post("/api/setup", json={"password": PASSWORD}).raise_for_status()
        c.post("/api/machine-access", json={"password": PASSWORD}).raise_for_status()
        c.post("/api/lock").raise_for_status()
        assert not MachineAccess(path).enabled
        c.post("/api/unlock", json={"credential": PASSWORD}).raise_for_status()
        c.post("/api/machine-access", json={"password": PASSWORD}).raise_for_status()
        c.post("/api/password", json={"password": "another-fictional-password"}).raise_for_status()
        assert not MachineAccess(path).enabled


def test_bad_device_blob_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    path = tmp_path / "fictional.bfin"
    store = MachineAccess(path)
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(b"corrupt fictional blob")
    with TestClient(create_personal_app(path), base_url="http://127.0.0.1:8899") as c:
        assert not c.get("/api/status").json()["unlocked"]


def test_wrong_password_does_not_enroll_or_log_out_session(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    path = tmp_path / "fictional.bfin"
    with TestClient(create_personal_app(path), base_url="http://127.0.0.1:8899") as c:
        c.post("/api/setup", json={"password": PASSWORD}).raise_for_status()
        response = c.post("/api/machine-access", json={"password": "wrong-fictional-password"})
        assert response.status_code == 401
        assert not MachineAccess(path).enabled
        assert c.get("/api/accounts").status_code == 200


def test_failed_credential_save_keeps_existing_session(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    path = tmp_path / "fictional.bfin"
    app = create_personal_app(path)
    with TestClient(app, base_url="http://127.0.0.1:8899", raise_server_exceptions=False) as c:
        c.post("/api/setup", json={"password": PASSWORD}).raise_for_status()

        def fail(credential):
            raise OSError("fictional disk failure")

        monkeypatch.setattr(app.state.personal.machine_access, "save", fail)
        assert c.post("/api/machine-access", json={"password": PASSWORD}).status_code == 500
        assert c.get("/api/accounts").status_code == 200
        assert not app.state.personal.background_access


def test_shutdown_requires_desktop_and_revokes_grant(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    path = tmp_path / "fictional.bfin"
    app = create_personal_app(path)
    exits = []
    app.state.request_exit = lambda: exits.append(True)
    with TestClient(app, base_url="http://127.0.0.1:8899") as c:
        assert c.post("/api/shutdown").status_code == 401
        c.post("/api/setup", json={"password": PASSWORD}).raise_for_status()
        c.post("/api/machine-access", json={"password": PASSWORD}).raise_for_status()
        assert c.post("/api/shutdown").status_code == 200
        assert exits == [True]
        assert not MachineAccess(path).enabled
        assert not app.state.personal.vault.unlocked


def test_logon_task_requires_authorized_background_access(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    path = tmp_path / "fictional.bfin"
    access = MachineAccess(path)
    registered = {"value": False}
    monkeypatch.setattr(
        MachineAccess, "install_logon_task", lambda self: registered.update(value=True)
    )
    monkeypatch.setattr(
        MachineAccess, "remove_logon_task", lambda self: registered.update(value=False)
    )
    monkeypatch.setattr(MachineAccess, "logon_task_enabled", lambda self: registered["value"])
    from app.features.personal import machine_access

    monkeypatch.setattr(machine_access, "protect", lambda value, decrypt=False: value)
    with TestClient(create_personal_app(path), base_url="http://127.0.0.1:8899") as c:
        assert c.get("/api/machine-access/logon-task").status_code == 401
        c.post("/api/setup", json={"password": PASSWORD}).raise_for_status()
        assert c.post("/api/machine-access/logon-task").status_code == 422
        c.post("/api/machine-access", json={"password": PASSWORD}).raise_for_status()
        assert c.get("/api/machine-access/logon-task").json() == {"enabled": False}
        assert c.post("/api/machine-access/logon-task").json() == {"enabled": True}
        assert access.logon_task_enabled()
        assert c.delete("/api/machine-access/logon-task").json() == {"enabled": False}
        assert not access.logon_task_enabled()


def test_logon_task_command_targets_this_vault_and_current_user(tmp_path):
    access = MachineAccess(tmp_path / "Fictional vault.bfin", tmp_path / "device")
    script = access._registration_script()
    assert "New-ScheduledTaskTrigger -AtLogOn" in script
    assert str(access.vault) in script
    assert "pythonw.exe" in script
    assert "-keep-running" in script
    assert "Register-ScheduledTask" in script
