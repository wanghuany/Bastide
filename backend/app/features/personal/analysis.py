"""Deterministic local Python reports; no model, network, eval, or arbitrary SQL."""

from collections import defaultdict
from datetime import date
from typing import Literal, TypedDict

from app.core.errors import ValidationError
from app.core.months import month_index, month_key
from app.features.personal.repository import LedgerRepository

type Report = Literal[
    "summary", "monthly", "annual", "category_ranking", "merchant_ranking", "year_over_year"
]
REPORTS = ("summary", "monthly", "annual", "category_ranking", "merchant_ranking", "year_over_year")


class MonthlyBucket(TypedDict):
    period: str
    income_minor: int
    expense_minor: int
    net_minor: int
    personal_expense_minor: int
    non_personal_expense_minor: int


def run_report(
    repo: LedgerRepository,
    report: str,
    start: date,
    end: date,
    category_id: str | None = None,
    account_id: str | None = None,
    expense_scope: str = "all",
) -> dict:
    """Compute locally and return only aggregates in integer minor units.

    Transfers never count as income/expense. Parent-category filters include children.
    A previous-period comparison uses the same calendar dates one year earlier.
    """
    if report not in REPORTS:
        raise ValidationError("不支持的报表，请使用允许的报表名称。")
    if expense_scope not in ("all", "personal"):
        raise ValidationError("支出口径只能为 all 或 personal。")
    if start > end or (end - start).days > 3660:
        raise ValidationError("请选择有效的日期区间，最长十年。")
    categories = {item["id"]: item for item in repo.categories()}
    months: dict[str, MonthlyBucket] = {
        month_key(i): {
            "period": month_key(i),
            "income_minor": 0,
            "expense_minor": 0,
            "net_minor": 0,
            "personal_expense_minor": 0,
            "non_personal_expense_minor": 0,
        }
        for i in range(month_index(start), month_index(end) + 1)
    }
    expense_by_category: dict[str, int] = defaultdict(int)
    expense_by_merchant: dict[str, int] = defaultdict(int)
    income = expense = count = personal_expense = non_personal_expense = 0
    rows = repo.analysis_rows(start, end, category_id, account_id)
    for booked, amount, category, merchant in rows:
        included = categories.get(category, {}).get("personal_expense_included", True)
        bucket = months[booked.strftime("%Y-%m")]
        if amount < 0:
            if included:
                personal_expense -= amount
                bucket["personal_expense_minor"] -= amount
            else:
                non_personal_expense -= amount
                bucket["non_personal_expense_minor"] -= amount
            if expense_scope == "personal" and not included:
                continue
        count += 1
        bucket = months[booked.strftime("%Y-%m")]
        if amount < 0:
            expense -= amount
            bucket["expense_minor"] -= amount
            item = categories.get(category)
            parent = categories.get(item["parent_id"]) if item and item["parent_id"] else item
            expense_by_category[parent["name"] if parent else "未分类"] -= amount
            expense_by_merchant[merchant or "未填写商家"] -= amount
        else:
            income += amount
            bucket["income_minor"] += amount
    for bucket in months.values():
        bucket["net_minor"] = bucket["income_minor"] - bucket["expense_minor"]

    def ranking(values: dict[str, int]) -> list[dict]:
        return [
            {"name": name, "amount_minor": amount}
            for name, amount in sorted(values.items(), key=lambda item: (-item[1], item[0]))[:20]
        ]

    summary = {
        "currency": "CNY",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "income_minor": income,
        "expense_minor": expense,
        "personal_expense_minor": personal_expense,
        "non_personal_expense_minor": non_personal_expense,
        "total_expense_minor": personal_expense + non_personal_expense,
        "expense_scope": expense_scope,
        "net_minor": income - expense,
        "transaction_count": count,
        "monthly": list(months.values()),
        "categories": ranking(expense_by_category),
        "merchants": ranking(expense_by_merchant),
    }
    if report == "summary":
        summary["accounts"] = repo.accounts()
        return summary
    base = {
        "report": report,
        "currency": "CNY",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "expense_scope": expense_scope,
    }
    if report == "monthly":
        return {**base, "items": summary["monthly"]}
    if report == "annual":
        years: dict[str, dict] = {}
        for item in months.values():
            year = item["period"][:4]
            value = years.setdefault(
                year,
                {
                    "period": year,
                    "income_minor": 0,
                    "expense_minor": 0,
                    "net_minor": 0,
                    "personal_expense_minor": 0,
                    "non_personal_expense_minor": 0,
                },
            )
            for key in (
                "income_minor",
                "expense_minor",
                "net_minor",
                "personal_expense_minor",
                "non_personal_expense_minor",
            ):
                value[key] += item[key]
        return {**base, "items": list(years.values())}
    if report == "category_ranking":
        return {**base, "items": summary["categories"], "expense_minor": expense}
    if report == "merchant_ranking":
        return {**base, "items": summary["merchants"], "expense_minor": expense}

    def previous_year(day: date) -> date:
        try:
            return day.replace(year=day.year - 1)
        except ValueError:
            return date(day.year - 1, 2, 28)

    previous_start, previous_end = previous_year(start), previous_year(end)
    previous_expense = sum(
        -amount
        for _, amount, category, _ in repo.analysis_rows(
            previous_start, previous_end, category_id, account_id
        )
        if amount < 0
        and (
            expense_scope == "all"
            or categories.get(category, {}).get("personal_expense_included", True)
        )
    )
    return {
        **base,
        "expense_minor": expense,
        "previous_expense_minor": previous_expense,
        "previous_start": previous_start.isoformat(),
        "previous_end": previous_end.isoformat(),
        "change_basis_points": ((expense - previous_expense) * 10000 // previous_expense)
        if previous_expense
        else None,
        "highest_month": max(months.values(), key=lambda item: item["expense_minor"])
        if count
        else None,
    }
