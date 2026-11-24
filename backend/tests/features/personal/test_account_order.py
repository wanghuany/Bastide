"""Account organization persists in the encrypted vault, without changing financial data."""

import pytest

from tests.features.personal.test_personal import PASSWORD, accounts, create_entry, report
from tests.features.personal.test_personal import personal as personal


def move(client, account_id, direction):
    response = client.post(f"/api/accounts/{account_id}/move", json={"direction": direction})
    assert response.status_code == 200, response.text
    return response.json()


def test_order_survives_reopen_without_changing_finances(personal):
    create_entry(personal)
    before = accounts(personal)
    totals = report(personal)
    expected = [a["id"] for a in before]
    expected[0], expected[1] = expected[1], expected[0]
    result = move(personal, before[1]["id"], "up")
    assert [a["id"] for a in result] == expected
    assert {a["id"]: a for a in result} == {a["id"]: a for a in before}
    assert report(personal)["expense_minor"] == totals["expense_minor"]
    personal.post("/api/lock")
    personal.post("/api/unlock", json={"credential": PASSWORD}).raise_for_status()
    assert [a["id"] for a in accounts(personal)] == expected
    assert move(personal, expected[0], "up") == accounts(personal)
    assert move(personal, expected[-1], "down") == accounts(personal)


def test_archive_groups_and_restore_preserve_history(personal):
    initial = accounts(personal)
    create_entry(personal, account_id=initial[0]["id"])
    expense = report(personal)["expense_minor"]
    for a in initial[:2]:
        personal.put(
            f"/api/accounts/{a['id']}",
            json={"name": a["name"], "archived": True},
        ).raise_for_status()
    grouped = accounts(personal)
    assert [a["archived"] for a in grouped] == [False] * (len(initial) - 2) + [True, True]
    result = move(personal, initial[1]["id"], "up")
    assert [a["id"] for a in result[-2:]] == [initial[1]["id"], initial[0]["id"]]
    assert move(personal, initial[1]["id"], "up") == result
    assert move(personal, result[-3]["id"], "down") == result
    a = initial[0]
    personal.put(f"/api/accounts/{a['id']}", json={"name": a["name"]}).raise_for_status()
    assert next(a for a in accounts(personal) if a["archived"])["id"] == initial[1]["id"]
    assert report(personal)["expense_minor"] == expense
    assert (
        next(a for a in accounts(personal) if a["id"] == initial[0]["id"])["cash_balance_minor"]
        == -1250
    )


@pytest.mark.parametrize("payload", [{"direction": "left"}, {}, {"direction": "up", "extra": 1}])
def test_move_validation(personal, payload):
    before = accounts(personal)
    assert personal.post(f"/api/accounts/{before[0]['id']}/move", json=payload).status_code == 422
    assert accounts(personal) == before


def test_move_requires_desktop_auth_and_existing_account(personal):
    assert personal.post("/api/accounts/missing/move", json={"direction": "up"}).status_code == 404
    account_id = accounts(personal)[0]["id"]
    personal.post("/api/lock")
    assert (
        personal.post(f"/api/accounts/{account_id}/move", json={"direction": "up"}).status_code
        == 401
    )
