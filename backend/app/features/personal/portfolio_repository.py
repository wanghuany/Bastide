"""Manual holdings, cash-backed fund movements, and valuation observations."""

from datetime import date

from sqlalchemy import select

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.features.accounts.balance import apply_delta
from app.features.personal.models import PersonalHolding, PersonalHoldingEvent
from app.features.personal.repository import LedgerRepository, snapshot
from app.features.personal.schemas import (
    EntryInput,
    HoldingInput,
    SavingsTransferInput,
    TradeInput,
    ValuationInput,
)
from app.features.transactions.models import Transaction


class PortfolioRepository(LedgerRepository):
    """Queries remain in the repository, using the same encrypted transaction."""

    def holding(self, holding_id: str) -> PersonalHolding:
        value = self.db.get(PersonalHolding, holding_id)
        if value is None:
            raise NotFoundError("基金持仓不存在。")
        self.account(value.account_id)
        return value

    def _holdings(self, account_id: str) -> list[PersonalHolding]:
        self.account(account_id)
        return list(
            self.db.scalars(
                select(PersonalHolding)
                .where(PersonalHolding.account_id == account_id)
                .order_by(PersonalHolding.name)
            )
        )

    def _latest(self, holding_id: str) -> PersonalHoldingEvent | None:
        return self.db.scalar(
            select(PersonalHoldingEvent)
            .where(
                PersonalHoldingEvent.holding_id == holding_id,
                PersonalHoldingEvent.undone.is_(False),
            )
            .order_by(
                PersonalHoldingEvent.booked_date.desc(),
                PersonalHoldingEvent.created_at.desc(),
                PersonalHoldingEvent.id.desc(),
            )
            .limit(1)
        )

    def _date(self, holding: PersonalHolding, booked: date) -> None:
        latest = self._latest(holding.id)
        if booked > date.today() or booked < (
            latest.booked_date if latest else holding.opening_date
        ):
            raise ValidationError("请按日期顺序记录，日期不能早于已有持仓记录，也不能晚于今天。")

    def add_holding(self, account_id: str, data: HoldingInput) -> dict:
        def operation() -> dict:
            self.account(account_id, active=True)
            existing = self._holdings(account_id)
            if len(existing) >= 200:
                raise ValidationError("单账户最多登记 200 个基金。")
            if any(x.name == data.name for x in existing):
                raise ConflictError("该账户已有同名基金。")
            if data.opening_date > date.today():
                raise ValidationError("期初持仓日期不能晚于今天。")
            value = PersonalHolding(
                account_id=account_id,
                name=data.name,
                opening_date=data.opening_date,
                opening_value_minor=data.opening_value_minor,
                current_value_minor=data.opening_value_minor,
            )
            self.db.add(value)
            self.db.flush()
            return {"holding_id": value.id}

        return self._idempotent(
            data.request_id,
            {"op": "holding", "account_id": account_id, **data.model_dump(mode="json")},
            operation,
        )

    def record_values(self, account_id: str, data: ValuationInput) -> dict:
        def operation() -> dict:
            self.account(account_id, active=True)
            positions = {x.id: x for x in self._holdings(account_id)}
            ids = [x.holding_id for x in data.holdings]
            if len(ids) != len(set(ids)) or set(ids) != set(positions):
                raise ValidationError("请一次填写该账户全部基金的最新金额，不要重复或漏填。")
            events = []
            gain = 0
            for item in data.holdings:
                holding = positions[item.holding_id]
                self._date(holding, data.booked_date)
                duplicate = self.db.scalar(
                    select(PersonalHoldingEvent.id).where(
                        PersonalHoldingEvent.holding_id == holding.id,
                        PersonalHoldingEvent.kind == "valuation",
                        PersonalHoldingEvent.booked_date == data.booked_date,
                        PersonalHoldingEvent.undone.is_(False),
                    )
                )
                if duplicate:
                    raise ConflictError("这一天已有估值；如录错，请先撤销对应记录再重新填写。")
                delta = item.value_minor - holding.current_value_minor
                event = PersonalHoldingEvent(
                    holding_id=holding.id,
                    kind="valuation",
                    booked_date=data.booked_date,
                    previous_value_minor=holding.current_value_minor,
                    value_minor=item.value_minor,
                    gain_minor=delta,
                    cash_amount_minor=0,
                )
                self.db.add(event)
                holding.current_value_minor = item.value_minor
                self.db.flush()
                events.append(event.id)
                gain += delta
            return {"event_ids": events, "gain_minor": gain}

        return self._idempotent(
            data.request_id,
            {"op": "valuation", "account_id": account_id, **data.model_dump(mode="json")},
            operation,
        )

    def trade(self, holding_id: str, data: TradeInput) -> dict:
        def operation() -> dict:
            holding = self.holding(holding_id)
            account = self.account(holding.account_id, active=True)
            self._date(holding, data.booked_date)
            if data.kind == "buy" and account.cached_balance_minor < data.amount_minor:
                raise ValidationError("账户现金不足，请先录入真实入金或账户间转入。")
            if data.kind == "sell" and holding.current_value_minor < data.amount_minor:
                raise ValidationError("赎回额超过当前估值，请先登记赎回前的真实市值再赎回。")
            amount = data.amount_minor if data.kind == "buy" else -data.amount_minor
            cash = -amount
            row = self._new_row(
                account,
                EntryInput(
                    account_id=account.id,
                    booked_date=data.booked_date,
                    kind="expense" if cash < 0 else "income",
                    amount_minor=data.amount_minor,
                    merchant=holding.name,
                    note="基金买入" if cash < 0 else "基金赎回",
                    request_id=data.request_id,
                ),
                cash,
                None,
                holding.name,
            )
            row.categorization_source, row.needs_review = "investment", False
            event = PersonalHoldingEvent(
                holding_id=holding.id,
                kind=data.kind,
                booked_date=data.booked_date,
                previous_value_minor=holding.current_value_minor,
                value_minor=holding.current_value_minor + amount,
                gain_minor=0,
                cash_amount_minor=cash,
                transaction_id=row.id,
            )
            holding.current_value_minor += amount
            self.db.add(event)
            self.db.flush()
            return {"event_id": event.id, "transaction_id": row.id, "cash_amount_minor": cash}

        return self._idempotent(
            data.request_id,
            {"op": "trade", "holding_id": holding_id, **data.model_dump(mode="json")},
            operation,
        )

    def undo_holding_event(self, event_id: str) -> dict:
        event = self.db.get(PersonalHoldingEvent, event_id)
        if event is None:
            raise NotFoundError("持仓记录不存在。")
        holding = self.holding(event.holding_id)
        account = self.account(holding.account_id, active=True)
        latest = self._latest(holding.id)
        if event.undone or latest is None or latest.id != event.id:
            raise ConflictError("只能撤销该基金最后一条有效记录，请先撤销后续记录。")
        row = self.get_entry(event.transaction_id) if event.transaction_id else None
        if row:
            if (
                row.categorization_source != "investment"
                or row.amount_minor != event.cash_amount_minor
            ):
                raise ConflictError("关联现金流水已变化，请先核对账本。")
            if account.cached_balance_minor - row.amount_minor < 0:
                raise ConflictError("撤销会导致现金不足，请先处理后续现金使用。")
            apply_delta(account, -row.amount_minor)
            event.transaction_id = None
            self.db.flush()
            self.db.delete(row)
        holding.current_value_minor = event.previous_value_minor
        event.undone = True
        self.db.flush()
        return {"undone": True, "account_id": account.id}

    def portfolio(self, account_id: str) -> dict:
        self.account(account_id)
        positions = self._holdings(account_id)
        events = list(
            self.db.scalars(
                select(PersonalHoldingEvent)
                .join(PersonalHolding)
                .where(
                    PersonalHolding.account_id == account_id,
                )
                .order_by(
                    PersonalHoldingEvent.booked_date.desc(),
                    PersonalHoldingEvent.created_at.desc(),
                    PersonalHoldingEvent.id.desc(),
                )
                .limit(500)
            )
        )
        result = next(x for x in self.accounts() if x["id"] == account_id)
        result["holdings"] = [
            {
                "id": x.id,
                "name": x.name,
                "opening_date": x.opening_date.isoformat(),
                "opening_value_minor": x.opening_value_minor,
                "value_minor": x.current_value_minor,
                "as_of": (
                    last.booked_date if (last := self._latest(x.id)) else x.opening_date
                ).isoformat(),
                "latest_event_id": last.id if last else None,
            }
            for x in positions
        ]
        names = {x.id: x.name for x in positions}
        result["events"] = [
            {
                "id": e.id,
                "holding_id": e.holding_id,
                "name": names[e.holding_id],
                "kind": e.kind,
                "booked_date": e.booked_date.isoformat(),
                "previous_value_minor": e.previous_value_minor,
                "value_minor": e.value_minor,
                "gain_minor": e.gain_minor,
                "cash_amount_minor": e.cash_amount_minor,
                "undone": e.undone,
            }
            for e in events
        ]
        # Totals do not depend on the bounded display history.
        result["valuation_gain_minor"] = sum(
            self.db.scalars(
                select(PersonalHoldingEvent.gain_minor)
                .join(PersonalHolding)
                .where(
                    PersonalHolding.account_id == account_id, PersonalHoldingEvent.undone.is_(False)
                )
            )
        )
        result["allowance"] = self.allowance(account_id, date.today())
        return result

    def allowance(self, account_id: str, on: date) -> dict:
        from app.features.personal.analysis import run_report

        account = self.account(account_id)
        profile = self.setting("account_profile:" + account_id)
        budget = profile.value.get("monthly_allowance_minor", 0) if profile else 0
        start = on.replace(day=1)
        summary = run_report(self, "summary", start, on, account_id=account_id)
        # Count actual paired transfers into savings accounts, so repeated sweeps
        # don't propose saving the same month's remainder again.
        savings = [x["id"] for x in self.accounts() if x["purpose"] == "savings"]
        saved = 0
        outbound = list(
            self.db.scalars(
                select(Transaction).where(
                    Transaction.account_id == account_id,
                    Transaction.categorization_source == "transfer",
                    Transaction.amount_minor < 0,
                    Transaction.booked_date >= start,
                    Transaction.booked_date <= on,
                )
            )
        )
        if outbound and savings:
            counterpart_ids = [(r.fitid or "").removesuffix(":out") + ":in" for r in outbound]
            saved = sum(
                self.db.scalars(
                    select(Transaction.amount_minor).where(
                        Transaction.account_id.in_(savings),
                        Transaction.fitid.in_(counterpart_ids),
                        Transaction.categorization_source == "transfer",
                    )
                )
            )
        remaining = max(0, budget - summary["personal_expense_minor"] - saved) if budget else 0
        return {
            "month": on.strftime("%Y-%m"),
            "budget_minor": budget,
            "personal_expense_minor": summary["personal_expense_minor"],
            "saved_minor": saved,
            "remaining_minor": remaining,
            "suggested_transfer_minor": min(max(0, account.cached_balance_minor), remaining),
            "budget_configured": bool(budget),
        }

    def portfolio_export(self) -> dict:
        return {
            "holdings": [
                {"account_id": a["id"], **self.portfolio(a["id"])} for a in self.accounts()
            ]
        }

    def savings_transfer(self, account_id: str, data: SavingsTransferInput) -> dict:
        def operation() -> dict:
            source = self.account(account_id, active=True)
            target = self.account(data.destination_account_id, active=True)
            profiles = {a["id"]: a for a in self.accounts()}
            if (
                profiles[source.id]["purpose"] not in ("allowance", "stock_allowance")
                or profiles[target.id]["purpose"] != "savings"
            ):
                raise ValidationError("结余转存需要从零花钱账户转入理财通/储蓄用途账户。")
            if source.cached_balance_minor < data.amount_minor:
                raise ValidationError("现金不足，不能自动赎回基金。请先核对现金并登记实际赎回。")
            if data.booked_date > date.today():
                raise ValidationError("只登记已发生的真实转存，日期不能晚于今天。")
            rows = self._insert(
                EntryInput(
                    account_id=source.id,
                    destination_account_id=target.id,
                    kind="transfer",
                    booked_date=data.booked_date,
                    amount_minor=data.amount_minor,
                    note="零花钱结余转存",
                    request_id=data.request_id,
                )
            )
            return {
                "transaction_ids": [r.id for r in rows],
                "action_id": self._audit("create", [], [snapshot(r) for r in rows]),
            }

        return self._idempotent(
            data.request_id,
            {"op": "savings_transfer", "account_id": account_id, **data.model_dump(mode="json")},
            operation,
        )

    def delete_initial_holding(self, holding_id: str) -> dict:
        value = self.holding(holding_id)
        self.account(value.account_id, active=True)
        if self.db.scalar(
            select(PersonalHoldingEvent.id)
            .where(PersonalHoldingEvent.holding_id == holding_id)
            .limit(1)
        ):
            raise ConflictError("已有变动历史的基金不能删除，以保留完整记录；清仓请登记实际赎回。")
        self.db.delete(value)
        return {"deleted": True}
