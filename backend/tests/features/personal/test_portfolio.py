"""Fictional fund observations and cash flows share one encrypted ledger."""

from datetime import date
from uuid import uuid4

import pytest

from tests.features.personal.test_personal import (
    PASSWORD,
    accounts,
    create_entry,
    entry,
    report,
)
from tests.features.personal.test_personal import (
    personal as personal,
)


def account_by_name(client, name="支付宝债券账户"):
    return next(a for a in accounts(client) if a["name"] == name)


def configure(client, account, cash=60000, **extra):
    response = client.put(
        "/api/accounts/" + account["id"],
        json={
            "name": account["name"],
            "opening_balance_minor": cash,
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def add_holding(client, account, amount=40000, name="虚构基金甲", opening="2026-11-01"):
    response = client.post(
        f"/api/accounts/{account['id']}/holdings",
        json={
            "name": name,
            "opening_date": opening,
            "opening_value_minor": amount,
            "request_id": str(uuid4()),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["holding_id"]


def portfolio(client, account):
    response = client.get(f"/api/accounts/{account['id']}/portfolio")
    assert response.status_code == 200, response.text
    return response.json()


def test_account_plan_is_idempotent_and_preserves_archives(personal):
    all_accounts = accounts(personal)
    expected = {
        "支付宝债券账户": "bond",
        "支付宝股票型基金零花钱账户": "stock_allowance",
        "微信理财通账户": "savings",
        "微信零花钱账户": "allowance",
    }
    assert {a["name"]: a["purpose"] for a in all_accounts if a["name"] in expected} == expected
    selected = account_by_name(personal)
    configure(personal, selected, archived=True)
    personal.post("/api/lock")
    assert personal.post("/api/unlock", json={"credential": PASSWORD}).status_code == 200
    assert account_by_name(personal)["archived"] is True
    assert len(accounts(personal)) == len(all_accounts)


def test_cumulative_source_includes_boundary_and_archived_history(personal):
    old = accounts(personal)[0]
    create_entry(personal, amount=1700, booked_date="2026-07-14")
    create_entry(personal, amount=2300, booked_date="2026-07-15")
    create_entry(personal, kind="income", amount=89000, booked_date="2026-07-15")
    create_entry(
        personal,
        kind="transfer",
        amount=1000,
        account_id=old["id"],
        destination_account_id=accounts(personal)[1]["id"],
        booked_date="2026-07-16",
    )
    configure(personal, old, cash=0, archived=True)
    result = personal.get("/api/finance/since-start").json()
    assert result["start"] == "2026-07-15"
    assert result["end"] == date.today().isoformat()
    assert result["expense_minor"] == 2300
    assert result["income_minor"] == 89000
    assert result["transaction_count"] == 2


def test_fund_cash_and_valuation_never_become_spending(personal):
    account = configure(personal, account_by_name(personal))
    holding = add_holding(personal, account)
    p = portfolio(personal, account)
    assert (p["balance_minor"], p["cash_balance_minor"], p["holdings_value_minor"]) == (
        100000,
        60000,
        40000,
    )
    buy = {
        "kind": "buy",
        "booked_date": "2026-11-05",
        "amount_minor": 10000,
        "request_id": str(uuid4()),
    }
    first = personal.post(f"/api/holdings/{holding}/trades", json=buy)
    assert first.status_code == 200, first.text
    replay = personal.post(f"/api/holdings/{holding}/trades", json=buy).json()
    assert replay["replayed"] is True
    assert replay["event_id"] == first.json()["event_id"]
    p = portfolio(personal, account)
    assert (p["balance_minor"], p["cash_balance_minor"], p["holdings_value_minor"]) == (
        100000,
        50000,
        50000,
    )
    values = {
        "booked_date": "2026-11-12",
        "holdings": [{"holding_id": holding, "value_minor": 52000}],
        "request_id": str(uuid4()),
    }
    saved = personal.post(f"/api/accounts/{account['id']}/valuations", json=values)
    assert saved.status_code == 200, saved.text
    assert saved.json()["gain_minor"] == 2000
    assert personal.post(f"/api/accounts/{account['id']}/valuations", json=values).json()[
        "replayed"
    ]
    sell = personal.post(
        f"/api/holdings/{holding}/trades",
        json={
            "kind": "sell",
            "booked_date": "2026-11-13",
            "amount_minor": 20000,
            "request_id": str(uuid4()),
        },
    )
    assert sell.status_code == 200, sell.text
    p = portfolio(personal, account)
    assert (
        p["balance_minor"],
        p["cash_balance_minor"],
        p["holdings_value_minor"],
        p["valuation_gain_minor"],
    ) == (102000, 70000, 32000, 2000)
    assert report(personal)["income_minor"] == report(personal)["expense_minor"] == 0
    assert personal.get("/api/finance/since-start").json()["transaction_count"] == 0
    cash_row = first.json()["transaction_id"]
    assert personal.delete("/api/transactions/" + cash_row).status_code == 422
    assert personal.put("/api/transactions/" + cash_row, json=entry(personal)).status_code == 422
    # A valuation can't be undone underneath a later sale.
    valuation_id = saved.json()["event_ids"][0]
    assert personal.post(f"/api/holding-events/{valuation_id}/undo").status_code == 409
    assert personal.post(f"/api/holding-events/{sell.json()['event_id']}/undo").status_code == 200
    assert personal.post(f"/api/holding-events/{valuation_id}/undo").status_code == 200
    p = portfolio(personal, account)
    assert p["balance_minor"] == 100000 and p["valuation_gain_minor"] == 0
    personal.post("/api/lock")
    personal.post("/api/unlock", json={"credential": PASSWORD})
    assert portfolio(personal, account)["holdings_value_minor"] == 50000


def test_weekly_values_require_complete_unique_chronological_amounts(personal):
    account = account_by_name(personal)
    a = add_holding(personal, account)
    b = add_holding(personal, account, name="虚构基金乙")
    path = f"/api/accounts/{account['id']}/valuations"

    def payload(values, day="2026-11-12"):
        return {"booked_date": day, "holdings": values, "request_id": str(uuid4())}

    assert (
        personal.post(path, json=payload([{"holding_id": a, "value_minor": 50000}])).status_code
        == 422
    )
    assert (
        personal.post(path, json=payload([{"holding_id": a, "value_minor": 50000}] * 2)).status_code
        == 422
    )
    valid = [{"holding_id": a, "value_minor": 42000}, {"holding_id": b, "value_minor": 39000}]
    assert personal.post(path, json=payload(valid)).json()["gain_minor"] == 1000
    assert personal.post(path, json=payload(valid)).status_code == 409
    assert personal.post(path, json=payload(valid, "2026-11-11")).status_code == 422
    assert personal.post(path, json=payload(valid, "2099-01-01")).status_code == 422
    assert (
        personal.post(
            path, json=payload([{**valid[0], "value_minor": 1.5}, valid[1]], "2026-11-13")
        ).status_code
        == 422
    )
    assert portfolio(personal, account)["holdings_value_minor"] == 81000


def test_fund_trade_guards_and_archived_accounts(personal):
    account = configure(personal, account_by_name(personal), cash=1000)
    holding = add_holding(personal, account, amount=5000)
    for kind, amount in (("buy", 1001), ("sell", 5001)):
        response = personal.post(
            f"/api/holdings/{holding}/trades",
            json={
                "kind": kind,
                "booked_date": "2026-11-12",
                "amount_minor": amount,
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 422
    assert (
        personal.post(
            "/api/transactions", json=entry(personal, account_id=account["id"])
        ).status_code
        == 422
    )
    configure(personal, account, cash=1000, archived=True)
    response = personal.post(
        f"/api/holdings/{holding}/trades",
        json={
            "kind": "buy",
            "booked_date": "2026-11-12",
            "amount_minor": 100,
            "request_id": str(uuid4()),
        },
    )
    assert response.status_code == 422
    assert portfolio(personal, account)["balance_minor"] == 6000


def test_monthly_allowance_savings_is_idempotent_and_not_income(personal):
    source = configure(
        personal,
        account_by_name(personal, "微信零花钱账户"),
        cash=30000,
        monthly_allowance_minor=10000,
    )
    target = account_by_name(personal, "微信理财通账户")
    create_entry(
        personal, amount=2500, account_id=source["id"], booked_date=date.today().isoformat()
    )
    budget = portfolio(personal, source)["allowance"]
    assert budget["suggested_transfer_minor"] == 7500
    data = {
        "destination_account_id": target["id"],
        "booked_date": date.today().isoformat(),
        "amount_minor": 7500,
        "request_id": str(uuid4()),
    }
    path = f"/api/accounts/{source['id']}/savings-transfers"
    moved = personal.post(path, json=data)
    assert moved.status_code == 200, moved.text
    assert personal.post(path, json=data).json()["replayed"]
    budget = portfolio(personal, source)["allowance"]
    assert budget["saved_minor"] == 7500 and budget["remaining_minor"] == 0
    assert account_by_name(personal, "微信理财通账户")["cash_balance_minor"] == 7500
    assert report(personal)["expense_minor"] == 2500 and report(personal)["income_minor"] == 0
    assert (
        personal.post(
            path, json={**data, "amount_minor": 900000, "request_id": str(uuid4())}
        ).status_code
        == 422
    )
    personal.post("/api/history/" + moved.json()["action_id"] + "/undo")
    assert portfolio(personal, source)["allowance"]["suggested_transfer_minor"] == 7500


def test_agent_portfolio_is_aggregate_and_write_scope_is_enforced(personal):
    account = account_by_name(personal)
    holding = add_holding(personal, account)
    token = personal.post("/api/agent-token", json={"allow_writes": False}).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    result = personal.get(f"/api/agent/accounts/{account['id']}/portfolio", headers=headers)
    assert result.status_code == 200 and "events" not in result.json()
    assert result.json()["holdings"][0]["value_minor"] == 40000
    assert personal.get("/api/agent/finance/since-start", headers=headers).status_code == 200
    data = {
        "booked_date": "2026-11-12",
        "holdings": [{"holding_id": holding, "value_minor": 41000}],
        "request_id": str(uuid4()),
    }
    assert (
        personal.post(
            f"/api/agent/accounts/{account['id']}/valuations", json=data, headers=headers
        ).status_code
        == 403
    )
    token = personal.post("/api/agent-token", json={"allow_writes": True}).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    response = personal.post(
        f"/api/agent/accounts/{account['id']}/valuations", json=data, headers=headers
    )
    assert response.status_code == 200 and response.json()["gain_minor"] == 1000
    exported = personal.get("/api/export?confirm_plaintext=true").json()
    assert any(p["holdings"] for p in exported["portfolio"]["holdings"])


def test_failed_portfolio_write_rolls_back_cash_and_position(personal, monkeypatch):
    from app.features.personal import vault as vault_module

    account = configure(personal, account_by_name(personal))
    holding = add_holding(personal, account)
    before = portfolio(personal, account)

    def fail(*args, **kwargs):
        raise OSError("fictional disk failure")

    monkeypatch.setattr(vault_module, "atomic_write", fail)
    with pytest.raises(OSError):
        from app.features.personal.schemas import TradeInput

        personal.app.state.personal.service.invoke(
            "trade",
            holding,
            TradeInput(
                kind="buy", booked_date="2026-11-12", amount_minor=1000, request_id=str(uuid4())
            ),
            write=True,
        )
    after = portfolio(personal, account)
    assert after["cash_balance_minor"] == before["cash_balance_minor"]
    assert after["holdings_value_minor"] == before["holdings_value_minor"]
    assert not after["events"]
