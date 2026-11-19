"""Exercise real migrations, encryption and HTTP boundaries using invented transactions."""

import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("mcp")

from fastapi.testclient import TestClient  # noqa: E402

from app.core.errors import AuthError, ConflictError  # noqa: E402
from app.features.personal import vault as vault_module  # noqa: E402
from app.features.personal.schemas import EntryInput  # noqa: E402
from app.features.personal.vault import MAGIC, Vault  # noqa: E402
from app.personal import create_personal_app  # noqa: E402

PASSWORD = "test-only-vault-password"
PERIOD = "start=2026-11-01&end=2026-11-30"


@pytest.fixture
def personal(tmp_path: Path):
    app = create_personal_app(tmp_path / "ledger.bfin")
    with TestClient(app, base_url="http://127.0.0.1:8899") as client:
        created = client.post("/api/setup", json={"password": PASSWORD})
        assert created.status_code == 200, created.text
        client.recovery = created.json()["recovery_key"]
        yield client


def accounts(client):
    response = client.get("/api/accounts")
    assert response.status_code == 200
    return response.json()


def category(client, name):
    return next(x for x in client.get("/api/categories").json() if x["name"] == name)["id"]


def entry(client, *, kind="expense", amount=1250, account_id=None, **fields):
    return {
        "account_id": account_id or accounts(client)[0]["id"],
        "booked_date": "2026-11-17",
        "kind": kind,
        "amount_minor": amount,
        "merchant": "测试早餐店",
        "note": "虚构测试数据",
        "request_id": str(uuid4()),
        **fields,
    }


def create_entry(client, data=None, **fields):
    response = client.post("/api/transactions", json=data or entry(client, **fields))
    assert response.status_code == 201, response.text
    return response.json()


def report(client):
    response = client.get(f"/api/analysis?{PERIOD}")
    assert response.status_code == 200, response.text
    return response.json()


def test_ciphertext_only_and_password_reopen(personal):
    create_entry(personal, merchant="PRIVATE_MERCHANT_93725", note="private-note-732")
    path = personal.app.state.personal.vault.path
    raw = path.read_bytes()
    assert raw.startswith(MAGIC)
    assert b"SQLite format 3" not in raw
    assert b"PRIVATE_MERCHANT_93725" not in raw
    assert b"private-note-732" not in raw
    assert {p.suffix for p in path.parent.iterdir()} == {".bfin", ".lock"}
    assert personal.post("/api/lock").status_code == 200
    assert personal.get("/api/accounts").status_code == 401
    assert personal.post("/api/unlock", json={"credential": PASSWORD}).status_code == 200
    assert report(personal)["expense_minor"] == 1250


def test_wrong_password_and_tampered_ciphertext(personal):
    raw = personal.app.state.personal.vault.path.read_bytes()
    personal.post("/api/lock")
    with pytest.raises(AuthError):
        Vault.decode(raw, "wrong-test-password")
    corrupt = raw[:-1] + bytes([raw[-1] ^ 1])
    with pytest.raises(AuthError):
        Vault.decode(corrupt, PASSWORD)
    assert personal.post("/api/unlock", json={"credential": PASSWORD}).status_code == 200


def test_recovery_and_password_rotation(personal):
    recovery = personal.recovery
    before = personal.get("/api/backup").content
    assert (
        personal.post("/api/password", json={"password": "new-test-vault-password"}).status_code
        == 200
    )
    changed = personal.get("/api/backup").content
    with pytest.raises(AuthError):
        Vault.decode(changed, PASSWORD)
    Vault.decode(before, PASSWORD)
    Vault.decode(changed, recovery, recovery=True)
    personal.post("/api/lock")
    assert (
        personal.post("/api/unlock", json={"credential": recovery, "recovery": True}).status_code
        == 200
    )


def test_password_spaces_are_significant(tmp_path):
    app = create_personal_app(tmp_path / "spaces.bfin")
    with TestClient(app, base_url="http://127.0.0.1:8899") as client:
        assert (
            client.post("/api/setup", json={"password": "  significant password  "}).status_code
            == 200
        )
        client.post("/api/lock")
        assert (
            client.post("/api/unlock", json={"credential": "  significant password  "}).status_code
            == 200
        )


def test_amounts_idempotency_and_balances(personal):
    data = entry(personal)
    first = create_entry(personal, data)
    second = create_entry(personal, data)
    assert second["replayed"] is True
    assert second["transaction_ids"] == first["transaction_ids"]
    assert accounts(personal)[0]["balance_minor"] == -1250
    create_entry(personal, kind="income", amount=800000)
    values = report(personal)
    assert values["expense_minor"] == 1250
    assert values["income_minor"] == 800000
    assert values["net_minor"] == 798750
    assert accounts(personal)[0]["balance_minor"] == 798750
    data["amount_minor"] += 1
    assert personal.post("/api/transactions", json=data).status_code == 409


@pytest.mark.parametrize("amount", [0, -1, 12.5, "1250", True, 9_000_000_000_001])
def test_invalid_money_never_writes(personal, amount):
    response = personal.post("/api/transactions", json=entry(personal, amount=amount))
    assert response.status_code == 422
    assert report(personal)["transaction_count"] == 0


def test_transfer_atomic_delete_undo_and_report_exclusion(personal):
    a, b = accounts(personal)[:2]
    result = create_entry(
        personal, kind="transfer", amount=25000, account_id=a["id"], destination_account_id=b["id"]
    )
    assert len(result["transaction_ids"]) == 2
    assert [x["balance_minor"] for x in accounts(personal)[:2]] == [-25000, 25000]
    assert report(personal)["transaction_count"] == 0
    deleted = personal.delete("/api/transactions/" + result["transaction_ids"][0])
    assert deleted.status_code == 200
    assert [x["balance_minor"] for x in accounts(personal)[:2]] == [0, 0]
    assert personal.post("/api/history/" + deleted.json()["action_id"] + "/undo").status_code == 200
    assert [x["balance_minor"] for x in accounts(personal)[:2]] == [-25000, 25000]


def test_update_and_undo_restore_exact_balance(personal):
    result = create_entry(personal)
    response = personal.put(
        "/api/transactions/" + result["transaction_ids"][0], json=entry(personal, amount=3456)
    )
    assert response.status_code == 200
    assert accounts(personal)[0]["balance_minor"] == -3456
    undo = personal.post("/api/history/" + response.json()["action_id"] + "/undo")
    assert undo.status_code == 200, undo.text
    assert accounts(personal)[0]["balance_minor"] == -1250
    assert personal.get("/api/transactions/" + result["transaction_ids"][0]).status_code == 200


def test_category_kind_cycle_and_account_archive_guards(personal):
    assert (
        personal.post(
            "/api/transactions", json=entry(personal, category_id=category(personal, "工资"))
        ).status_code
        == 422
    )
    cats = personal.get("/api/categories").json()
    meal = next(x for x in cats if x["name"] == "餐饮")
    child = next(x for x in cats if x["name"] == "早餐")
    assert (
        personal.put(
            "/api/categories/" + meal["id"], json={"name": "餐饮", "parent_id": child["id"]}
        ).status_code
        == 422
    )
    a = accounts(personal)[0]
    assert (
        personal.put(
            "/api/accounts/" + a["id"], json={"name": a["name"], "archived": True}
        ).status_code
        == 200
    )
    assert (
        personal.post("/api/transactions", json=entry(personal, account_id=a["id"])).status_code
        == 422
    )


def test_bulk_category_preview_commit_undo_and_conflict(personal):
    source, target = category(personal, "早餐"), category(personal, "午餐")
    created = create_entry(personal, category_id=source)
    preview = personal.post(
        "/api/categories/preview", json={"source_category_id": source, "target_category_id": target}
    ).json()
    assert preview["count"] == 1
    commit = personal.post("/api/categories/commit", json={"preview_id": preview["preview_id"]})
    assert commit.status_code == 200, commit.text
    assert (
        personal.post(
            "/api/categories/commit", json={"preview_id": preview["preview_id"]}
        ).status_code
        == 409
    )
    assert personal.post("/api/history/" + created["action_id"] + "/undo").status_code == 409
    assert personal.post("/api/history/" + commit.json()["action_id"] + "/undo").status_code == 200
    rows = personal.get("/api/transactions").json()["items"]
    assert rows[0]["category_id"] == source
    second = personal.post(
        "/api/categories/preview", json={"source_category_id": source, "target_category_id": target}
    ).json()
    personal.put(
        "/api/transactions/" + rows[0]["id"], json=entry(personal, amount=2200, category_id=source)
    )
    assert personal.post(
        "/api/categories/commit", json={"preview_id": second["preview_id"]}
    ).status_code in (404, 409)


def test_import_preview_dedup_review_and_undo(personal):
    text = (
        "微信支付账单明细\n交易时间,交易类型,交易对方,商品,收/支,金额(元),当前状态,交易单号\n"
        "2026-11-16 08:00:00,商户消费,虚构早餐店,早餐,支出,12.50,支付成功,test-order-001\n"
        "2026-11-17 09:00:00,转账,虚构联系人,转账,不计收支,100.00,支付成功,test-order-002\n"
        "2026-11-18 10:00:00,商户消费,虚构商店,退款,收入,5.00,已退款,test-order-003\n"
    )
    payload = {
        "account_id": accounts(personal)[0]["id"],
        "filename": "invented.csv",
        "format": "auto",
        "content_base64": base64.b64encode(text.encode("gb18030")).decode(),
    }
    invalid = personal.post(
        "/api/imports/preview", json={**payload, "content_base64": "not-valid-base64!"}
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["message"] == "文件编码无效，请重新选择 CSV 文件。"
    preview = personal.post("/api/imports/preview", json=payload)
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert (data["count"], data["skipped_count"], data["format"]) == (1, 2, "wechat")
    assert report(personal)["transaction_count"] == 0
    committed = personal.post("/api/imports/commit", json={"preview_id": data["preview_id"]})
    assert committed.status_code == 200, committed.text
    assert report(personal)["expense_minor"] == 1250
    again = personal.post("/api/imports/preview", json=payload).json()
    assert (again["count"], again["duplicates"]) == (0, 1)
    assert (
        personal.post("/api/history/" + committed.json()["action_id"] + "/undo").status_code == 200
    )
    assert accounts(personal)[0]["balance_minor"] == 0


def test_import_rechecks_duplicates_at_confirmation(personal):
    text = (
        "date,kind,amount,merchant,note,external_id\n"
        "2026-11-17,expense,13.00,fictional-cafe,coffee,sample-001\n"
    )
    payload = {
        "account_id": accounts(personal)[0]["id"],
        "filename": "test.csv",
        "content_base64": base64.b64encode(text.encode()).decode(),
    }
    first = personal.post("/api/imports/preview", json=payload).json()
    second = personal.post("/api/imports/preview", json=payload).json()
    assert (
        personal.post("/api/imports/commit", json={"preview_id": first["preview_id"]}).json()[
            "count"
        ]
        == 1
    )
    result = personal.post("/api/imports/commit", json={"preview_id": second["preview_id"]}).json()
    assert result["count"] == 0 and result["duplicates"] == 1


def test_backup_restoration_preserves_prior_ciphertext(personal):
    create_entry(personal)
    backup = personal.get("/api/backup").content
    create_entry(personal, amount=2222)
    path = personal.app.state.personal.vault.path
    newer = path.read_bytes()
    personal.post("/api/lock")
    result = personal.post(
        "/api/restore",
        json={"content_base64": base64.b64encode(backup).decode(), "credential": PASSWORD},
    )
    assert result.status_code == 200, result.text
    assert path.with_name("ledger.before-restore.bfin").read_bytes() == newer
    personal.post("/api/unlock", json={"credential": PASSWORD})
    assert report(personal)["expense_minor"] == 1250


def test_wrong_backup_password_never_overwrites(personal):
    path = personal.app.state.personal.vault.path
    before = path.read_bytes()
    personal.post("/api/lock")
    result = personal.post(
        "/api/restore",
        json={
            "content_base64": base64.b64encode(before).decode(),
            "credential": "wrong-test-password",
        },
    )
    assert result.status_code == 401
    assert path.read_bytes() == before


def test_failed_disk_commit_restores_memory_and_file(personal):
    create_entry(personal)
    path = personal.app.state.personal.vault.path
    before = path.read_bytes()
    with (
        patch.object(vault_module, "atomic_write", side_effect=OSError("invented disk full")),
        pytest.raises(OSError),
    ):
        personal.app.state.personal.service.invoke(
            "create_entry",
            EntryInput(**entry(personal, amount=7777)),
            write=True,
        )
    assert path.read_bytes() == before
    assert accounts(personal)[0]["balance_minor"] == -1250
    assert report(personal)["expense_minor"] == 1250


def test_sync_replacement_is_not_overwritten_and_memory_rolls_back(personal):
    create_entry(personal)
    path = personal.app.state.personal.vault.path
    earlier = path.read_bytes()
    create_entry(personal, amount=2222)
    # Simulate another computer's complete ciphertext arriving through file sync.
    path.write_bytes(earlier)
    response = personal.post("/api/transactions", json=entry(personal, amount=7777))
    assert response.status_code == 409
    assert path.read_bytes() == earlier
    assert report(personal)["expense_minor"] == 3472
    personal.post("/api/lock")
    assert personal.post("/api/unlock", json={"credential": PASSWORD}).status_code == 200
    assert report(personal)["expense_minor"] == 1250


def test_second_process_cannot_open_same_vault(personal):
    other = Vault(personal.app.state.personal.vault.path)
    with pytest.raises(ConflictError):
        other.unlock(PASSWORD)
    assert personal.app.state.personal.vault.unlocked


def test_origin_host_size_and_error_redaction(personal):
    assert (
        personal.post("/api/lock", headers={"Origin": "https://untrusted.invalid"}).status_code
        == 403
    )
    assert personal.get("/api/status", headers={"Host": "rebound.invalid:8899"}).status_code == 403
    assert (
        personal.post(
            "/api/unlock", content=b"{}", headers={"Content-Length": "5000001"}
        ).status_code
        == 413
    )
    response = personal.post("/api/password", json={"password": "sensitive"})
    assert response.status_code == 422
    assert "sensitive" not in response.text
    assert personal.get("/.personal-data/ledger.bfin").status_code == 404


def test_agent_scopes_rotation_raw_detail_gating_and_local_aggregation(personal):
    create_entry(personal, merchant="RAW_TEST_MERCHANT_891")
    issued = personal.post("/api/agent-token", json={"allow_writes": False}).json()
    headers = {"Authorization": "Bearer " + issued["token"], "Cookie": ""}
    summary = personal.get(f"/api/agent/analysis?{PERIOD}&report=monthly", headers=headers)
    assert summary.status_code == 200
    assert "RAW_TEST_MERCHANT_891" not in summary.text
    assert "transactions" not in summary.json()
    assert personal.get(f"/api/agent/transactions?{PERIOD}", headers=headers).status_code == 403
    assert personal.get("/api/transactions", headers=headers).status_code == 401
    # Re-authenticate desktop after testing a request with its cookie deliberately absent.
    personal.post("/api/unlock", json={"credential": PASSWORD})
    payload = entry(personal)
    assert (
        personal.post("/api/agent/transactions", json=payload, headers=headers).status_code == 403
    )
    next_token = personal.post("/api/agent-token", json={"allow_details": True}).json()["token"]
    assert personal.get("/api/agent/catalog", headers=headers).status_code == 401
    detail_headers = {"Authorization": "Bearer " + next_token, "Cookie": ""}
    assert (
        personal.get(f"/api/agent/transactions?{PERIOD}", headers=detail_headers).status_code == 200
    )
    personal.app.state.personal.last_activity -= 1000
    assert personal.get("/api/agent/catalog", headers=detail_headers).status_code == 401
    assert not personal.app.state.personal.vault.unlocked


def test_analysis_parent_filters_and_year_comparison(personal):
    create_entry(personal, amount=1234, category_id=category(personal, "早餐"))
    create_entry(
        personal, amount=3000, booked_date="2025-11-17", category_id=category(personal, "早餐")
    )
    parent = category(personal, "餐饮")
    value = personal.get(
        f"/api/analysis?{PERIOD}&report=year_over_year&category_id={parent}"
    ).json()
    assert value["expense_minor"] == 1234
    assert value["previous_expense_minor"] == 3000
    assert isinstance(value["change_basis_points"], int)
    assert personal.get(f"/api/analysis?{PERIOD}&report=execute_arbitrary_sql").status_code == 422


def test_mcp_tools_and_no_password_or_code_execution_tools(monkeypatch):
    from app.personal_mcp import call, commit_category_change, mcp

    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}
    assert {
        "create_transaction",
        "run_analysis",
        "preview_category_change",
        "commit_category_change",
    } <= names
    assert not any("password" in name or "sql" in name or "execute" in name for name in names)
    assert "CONFIRM_REQUIRED" in json.dumps(commit_category_change("invented-id"))
    monkeypatch.setenv("BASTIDE_PERSONAL_URL", "https://remote.invalid")
    assert call("GET", "/api/agent/catalog")["error"]["code"] == "INVALID_LOCAL_URL"


def test_export_requires_explicit_plaintext_confirmation(personal):
    assert personal.get("/api/export").status_code == 422
    value = personal.get("/api/export?confirm_plaintext=true")
    assert value.status_code == 200
    assert value.json()["format"] == "bastide-personal-export-v1"


def test_default_mode_remains_locked_on_startup(tmp_path):
    path = tmp_path / "fresh.bfin"
    app = create_personal_app(path)
    with TestClient(app, base_url="http://127.0.0.1:8899") as client:
        assert client.get("/api/status").json() == {
            "initialized": False,
            "unlocked": False,
            "authenticated": False,
            "idle_lock_minutes": 15,
        }
        assert client.get("/api/accounts").status_code == 401
        assert not path.exists()
        html = client.get("/")
        assert html.status_code == 200
        assert "content-security-policy" in html.headers
