"""Single-user ledger repository reusing Bastide's actual ORM and balance helpers."""

import hashlib
import json
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import AuthError, ConflictError, NotFoundError, ValidationError
from app.features.accounts.balance import apply_delta, shift_opening_balance
from app.features.accounts.models import Account
from app.features.auth.models import User
from app.features.categories.models import Category
from app.features.imports.canonical import clean_description, compute_dedup_hash
from app.features.imports.models import ImportBatch
from app.features.personal.models import PersonalAction, PersonalDraft, PersonalSetting
from app.features.personal.schemas import AccountInput, CategoryInput, EntryInput, RuleInput
from app.features.transactions.models import Transaction

USER_ID = "d4006395-b18a-4c46-9da4-2673857dafda"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def snapshot(row: Transaction) -> dict:
    result = {}
    for column in Transaction.__table__.columns:
        value = getattr(row, column.name)
        result[column.name] = value.isoformat() if isinstance(value, date | datetime) else value
    return result


def restore_values(value: dict) -> dict:
    result = dict(value)
    for key in ("booked_date", "value_date"):
        if result.get(key):
            result[key] = date.fromisoformat(result[key])
    for key in ("created_at", "updated_at"):
        if result.get(key):
            result[key] = datetime.fromisoformat(result[key])
    return result


class LedgerRepository:
    """All queries are scoped to the personal vault's one user."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def bootstrap(self) -> None:
        if self.db.get(User, USER_ID) is not None:
            return
        self.db.add(
            User(
                id=USER_ID,
                email="personal@localhost.invalid",
                display_name="personal",
                password_hash="!vault-managed!",
                locale="zh",
                currency="CNY",
            )
        )
        self.db.flush()
        for name in ("现金", "微信", "支付宝", "银行卡"):
            self.db.add(
                Account(
                    user_id=USER_ID,
                    name=name,
                    type="checking",
                    institution="本地账本",
                    currency="CNY",
                    opening_balance_minor=0,
                    cached_balance_minor=0,
                )
            )
        catalog = {
            "餐饮": ("早餐", "午餐", "晚餐", "咖啡饮品", "外卖"),
            "交通": ("公共交通", "打车", "加油停车"),
            "生活": ("买菜", "日用品", "住房水电"),
            "购物": ("数码", "服饰", "其他购物"),
            "休闲": ("娱乐", "旅行"),
            "健康": ("医疗", "运动"),
            "其他支出": (),
        }
        for index, (name, children) in enumerate(catalog.items()):
            parent = Category(
                user_id=USER_ID,
                name=name,
                kind="expense",
                icon="tag",
                color=f"cat{index % 7}",
                is_system=False,
            )
            self.db.add(parent)
            self.db.flush()
            for child in children:
                self.db.add(
                    Category(
                        user_id=USER_ID,
                        name=child,
                        kind="expense",
                        icon="tag",
                        color=parent.color,
                        parent_id=parent.id,
                        is_system=False,
                    )
                )
        for name in ("工资", "奖金", "退款", "其他收入"):
            self.db.add(
                Category(
                    user_id=USER_ID,
                    name=name,
                    kind="income",
                    icon="tag",
                    color="income",
                    is_system=False,
                )
            )
        self.db.flush()

    def accounts(self) -> list[dict]:
        return [
            {
                "id": x.id,
                "name": x.name,
                "currency": x.currency,
                "opening_balance_minor": x.opening_balance_minor,
                "balance_minor": x.cached_balance_minor,
                "archived": x.archived,
            }
            for x in self.db.scalars(
                select(Account).where(Account.user_id == USER_ID).order_by(Account.created_at)
            )
        ]

    def account(self, account_id: str, active: bool = False) -> Account:
        value = self.db.scalar(
            select(Account).where(Account.id == account_id, Account.user_id == USER_ID)
        )
        if value is None:
            raise NotFoundError("账户不存在。")
        if active and value.archived:
            raise ValidationError("归档账户不能新增账目。")
        return value

    def save_account(self, data: AccountInput, account_id: str | None = None) -> dict:
        duplicate = self.db.scalar(
            select(Account).where(Account.user_id == USER_ID, Account.name == data.name)
        )
        if duplicate is not None and duplicate.id != account_id:
            raise ConflictError("账户名称已存在。")
        if account_id is None:
            value = Account(
                user_id=USER_ID,
                name=data.name,
                type="checking",
                institution="本地账本",
                currency="CNY",
                opening_balance_minor=data.opening_balance_minor,
                cached_balance_minor=data.opening_balance_minor,
                archived=data.archived,
            )
            self.db.add(value)
        else:
            value = self.account(account_id)
            value.name, value.archived = data.name, data.archived
            shift_opening_balance(self.db, value, data.opening_balance_minor)
        self.db.flush()
        return next(x for x in self.accounts() if x["id"] == value.id)

    def categories(self) -> list[dict]:
        return [
            {"id": c.id, "parent_id": c.parent_id, "name": c.name, "kind": c.kind, "color": c.color}
            for c in self.db.scalars(
                select(Category)
                .where(Category.user_id == USER_ID)
                .order_by(Category.kind, Category.name)
            )
        ]

    def category(self, category_id: str) -> Category:
        value = self.db.scalar(
            select(Category).where(Category.id == category_id, Category.user_id == USER_ID)
        )
        if value is None:
            raise NotFoundError("分类不存在。")
        return value

    def save_category(self, data: CategoryInput, category_id: str | None = None) -> dict:
        parent = self.category(data.parent_id) if data.parent_id else None
        if parent and (parent.parent_id or parent.kind != data.kind or parent.id == category_id):
            raise ValidationError("分类最多两级，父子分类的收支类型必须一致。")
        value = self.category(category_id) if category_id else None
        if value and value.kind != data.kind:
            raise ValidationError("已有分类不能改变收支类型，请新建分类。")
        if (
            value
            and data.parent_id
            and self.db.scalar(select(Category.id).where(Category.parent_id == value.id).limit(1))
        ):
            raise ValidationError("带有子分类的一级分类不能移动到其他分类下。")
        duplicate = self.db.scalar(
            select(Category).where(
                Category.user_id == USER_ID,
                Category.name == data.name,
                Category.kind == data.kind,
                Category.parent_id == data.parent_id,
            )
        )
        if duplicate and duplicate.id != category_id:
            raise ConflictError("同一层级中已有同名分类。")
        if value is None:
            value = Category(user_id=USER_ID, icon="tag", is_system=False)
            self.db.add(value)
        value.name, value.kind, value.parent_id = data.name, data.kind, data.parent_id
        value.color = parent.color if parent else ("income" if data.kind == "income" else "cat2")
        self.db.flush()
        return next(x for x in self.categories() if x["id"] == value.id)

    def delete_category(self, category_id: str) -> None:
        value = self.category(category_id)
        if self.db.scalar(select(Category.id).where(Category.parent_id == category_id).limit(1)):
            raise ConflictError("请先处理子分类。")
        if self.db.scalar(
            select(Transaction.id).where(Transaction.category_id == category_id).limit(1)
        ):
            raise ConflictError("该分类仍有账目，请先批量迁移。")
        if any(r["category_id"] == category_id for r in self.rules()):
            raise ConflictError("该分类仍被自动分类规则引用。")
        self.db.delete(value)

    def setting(self, key: str) -> PersonalSetting | None:
        value = self.db.get(PersonalSetting, key)
        return value if value and value.user_id == USER_ID else None

    def set_setting(self, key: str, value: dict) -> None:
        row = self.setting(key)
        if row is None:
            self.db.add(PersonalSetting(key=key, user_id=USER_ID, value=value))
        else:
            row.value = value
        self.db.flush()

    def rules(self) -> list[dict]:
        return [
            {"id": row.key.removeprefix("rule:"), **row.value}
            for row in self.db.scalars(
                select(PersonalSetting)
                .where(PersonalSetting.user_id == USER_ID, PersonalSetting.key.startswith("rule:"))
                .order_by(PersonalSetting.key)
            )
        ]

    def save_rule(self, data: RuleInput) -> dict:
        self.category(data.category_id)
        rule_id = str(uuid4())
        self.set_setting("rule:" + rule_id, data.model_dump())
        return {"id": rule_id, **data.model_dump()}

    def delete_rule(self, rule_id: str) -> None:
        row = self.setting("rule:" + rule_id)
        if row is None:
            raise NotFoundError("规则不存在。")
        self.db.delete(row)

    def classify(self, merchant: str, note: str, kind: str) -> tuple[str | None, str]:
        text = (merchant + " " + note).casefold()
        for rule in self.rules():
            category = self.category(rule["category_id"])
            if category.kind == kind and rule["keyword"].casefold() in text:
                return category.id, rule["merchant"] or merchant
        suggestions = {
            "早餐": ("早餐", "包子"),
            "午餐": ("午餐", "午饭"),
            "晚餐": ("晚餐", "晚饭"),
            "咖啡饮品": ("咖啡", "星巴克", "瑞幸", "奶茶"),
            "外卖": ("外卖", "饿了么", "美团"),
            "打车": ("滴滴", "打车", "出租车"),
            "公共交通": ("地铁", "公交"),
            "买菜": ("买菜", "菜市场"),
            "工资": ("工资", "薪资"),
            "退款": ("退款",),
        }
        categories = self.categories()
        for name, words in suggestions.items():
            if any(word in text for word in words):
                match = next(
                    (x for x in categories if x["name"] == name and x["kind"] == kind), None
                )
                if match:
                    return match["id"], merchant
        return None, merchant

    def _batch(self, account: Account) -> ImportBatch:
        file_hash = "personal-manual-v1"
        batch = self.db.scalar(
            select(ImportBatch).where(
                ImportBatch.account_id == account.id, ImportBatch.file_hash == file_hash
            )
        )
        if batch is None:
            batch = ImportBatch(
                user_id=USER_ID,
                account_id=account.id,
                source_format="manual",
                file_name="本地记账",
                file_hash=file_hash,
                period_start=date(2000, 1, 1),
                period_end=date(9999, 12, 31),
                transaction_count=0,
                new_count=0,
                duplicate_count=0,
                status="done",
            )
            self.db.add(batch)
            self.db.flush()
        return batch

    def get_entry(self, entry_id: str) -> Transaction:
        row = self.db.scalar(
            select(Transaction)
            .join(Account)
            .where(Transaction.id == entry_id, Account.user_id == USER_ID)
        )
        if row is None:
            raise NotFoundError("账目不存在。")
        return row

    def group(self, entry_id: str) -> list[Transaction]:
        row = self.get_entry(entry_id)
        if row.categorization_source != "transfer":
            return [row]
        prefix = (row.fitid or "").rsplit(":", 1)[0] + ":"
        rows = list(
            self.db.scalars(
                select(Transaction)
                .join(Account)
                .where(Account.user_id == USER_ID, Transaction.fitid.startswith(prefix))
            )
        )
        if len(rows) != 2:
            raise ConflictError("转账配对不完整，请先恢复备份。")
        return rows

    def entry_detail(self, entry_id: str) -> dict:
        return {"items": [snapshot(row) for row in self.group(entry_id)]}

    def _new_row(
        self,
        account: Account,
        data: EntryInput,
        amount: int,
        category_id: str | None,
        merchant: str,
        fitid: str | None = None,
    ) -> Transaction:
        description = clean_description(
            merchant or data.note or ("转账" if data.kind == "transfer" else "手动记账")
        )
        row = Transaction(
            account_id=account.id,
            import_batch_id=self._batch(account).id,
            booked_date=data.booked_date,
            amount_minor=amount,
            currency=account.currency,
            description_raw=description,
            description_clean=description,
            memo=data.note or None,
            merchant=merchant or None,
            category_id=category_id,
            categorization_source="transfer" if data.kind == "transfer" else "user",
            needs_review=category_id is None and data.kind != "transfer",
            fitid=fitid,
            dedup_hash=compute_dedup_hash(account.id, data.booked_date, amount, description),
        )
        self.db.add(row)
        apply_delta(account, amount)
        self.db.flush()
        return row

    def _insert(self, data: EntryInput) -> list[Transaction]:
        account = self.account(data.account_id, active=True)
        category_id, merchant = data.category_id, data.merchant
        if category_id and self.category(category_id).kind != data.kind:
            raise ValidationError("所选分类与收支类型不一致。")
        if not category_id and data.kind != "transfer":
            category_id, merchant = self.classify(merchant, data.note, data.kind)
        if data.kind == "transfer":
            destination = self.account(data.destination_account_id or "", active=True)
            group_id = str(uuid4())
            return [
                self._new_row(
                    account, data, -data.amount_minor, None, merchant, f"transfer:{group_id}:out"
                ),
                self._new_row(
                    destination, data, data.amount_minor, None, merchant, f"transfer:{group_id}:in"
                ),
            ]
        sign = -1 if data.kind == "expense" else 1
        return [self._new_row(account, data, data.amount_minor * sign, category_id, merchant)]

    def _audit(self, kind: str, before: list[dict], after: list[dict]) -> str:
        action = PersonalAction(
            user_id=USER_ID, kind=kind, payload={"before": before, "after": after}
        )
        self.db.add(action)
        self.db.flush()
        return action.id

    def _idempotent(self, request_id: str, payload: dict, operation: Any) -> dict:
        key = "request:" + request_id
        previous = self.setting(key)
        signature = digest(payload)
        if previous is not None:
            if previous.value["hash"] != signature:
                raise ConflictError("同一 request_id 不能用于不同操作。")
            return {**previous.value["result"], "replayed": True}
        result = operation()
        self.set_setting(key, {"hash": signature, "result": result})
        return result

    def create_entry(self, data: EntryInput) -> dict:
        def operation() -> dict:
            rows = self._insert(data)
            action = self._audit("create", [], [snapshot(row) for row in rows])
            return {
                "transaction_ids": [row.id for row in rows],
                "action_id": action,
                "amount_minor": data.amount_minor,
                "kind": data.kind,
            }

        return self._idempotent(
            data.request_id, {"op": "create", **data.model_dump(mode="json")}, operation
        )

    def update_entry(self, entry_id: str, data: EntryInput) -> dict:
        def operation() -> dict:
            old = self.group(entry_id)
            before = [snapshot(row) for row in old]
            for row in old:
                apply_delta(self.account(row.account_id), -row.amount_minor)
                self.db.delete(row)
            self.db.flush()
            rows = self._insert(data)
            action = self._audit("update", before, [snapshot(row) for row in rows])
            return {"transaction_ids": [row.id for row in rows], "action_id": action}

        return self._idempotent(
            data.request_id,
            {"op": "update", "id": entry_id, **data.model_dump(mode="json")},
            operation,
        )

    def delete_entry(self, entry_id: str) -> dict:
        rows = self.group(entry_id)
        before = [snapshot(row) for row in rows]
        for row in rows:
            apply_delta(self.account(row.account_id), -row.amount_minor)
            self.db.delete(row)
        self.db.flush()
        return {"action_id": self._audit("delete", before, [])}

    def entries(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        category_id: str | None = None,
        account_id: str | None = None,
        q: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        query = select(Transaction).join(Account).where(Account.user_id == USER_ID)
        if start:
            query = query.where(Transaction.booked_date >= start)
        if end:
            query = query.where(Transaction.booked_date <= end)
        if category_id:
            descendants = [
                category_id,
                *self.db.scalars(
                    select(Category.id).where(
                        Category.user_id == USER_ID, Category.parent_id == category_id
                    )
                ),
            ]
            query = query.where(Transaction.category_id.in_(descendants))
        if account_id:
            query = query.where(Transaction.account_id == account_id)
        if q:
            query = query.where(
                or_(
                    Transaction.description_clean.contains(q, autoescape=True),
                    Transaction.memo.contains(q, autoescape=True),
                )
            )
        total = self.db.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self.db.scalars(
            query.order_by(Transaction.booked_date.desc(), Transaction.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return {
            "items": [snapshot(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def analysis_rows(
        self, start: date, end: date, category_id: str | None = None, account_id: str | None = None
    ):
        """Stream local report inputs; no transaction details cross the API boundary."""
        query = (
            select(
                Transaction.booked_date,
                Transaction.amount_minor,
                Transaction.category_id,
                Transaction.merchant,
            )
            .join(Account)
            .where(
                Account.user_id == USER_ID,
                Transaction.categorization_source != "transfer",
                Transaction.booked_date >= start,
                Transaction.booked_date <= end,
            )
        )
        if category_id:
            self.category(category_id)
            children = self.db.scalars(
                select(Category.id).where(
                    Category.user_id == USER_ID, Category.parent_id == category_id
                )
            )
            query = query.where(Transaction.category_id.in_([category_id, *children]))
        if account_id:
            self.account(account_id)
            query = query.where(Transaction.account_id == account_id)
        return self.db.execute(query).yield_per(1000)

    def preview_category(self, source_id: str, target_id: str) -> dict:
        source, target = self.category(source_id), self.category(target_id)
        if source.id == target.id or source.kind != target.kind:
            raise ValidationError("请选择不同且收支类型一致的分类。")
        rows = list(
            self.db.scalars(
                select(Transaction)
                .join(Account)
                .where(Account.user_id == USER_ID, Transaction.category_id == source_id)
                .limit(5001)
            )
        )
        if len(rows) > 5000:
            raise ValidationError("一次最多调整 5000 笔账目。")
        before = [snapshot(row) for row in rows]
        draft = self.draft(
            "category", {"source_id": source_id, "target_id": target_id, "before": before}
        )
        return {
            "preview_id": draft.id,
            "count": len(rows),
            "source": source.name,
            "target": target.name,
        }

    def draft(self, kind: str, payload: dict) -> PersonalDraft:
        value = PersonalDraft(
            user_id=USER_ID,
            kind=kind,
            payload=payload,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        self.db.add(value)
        self.db.flush()
        return value

    def get_draft(self, draft_id: str, kind: str) -> PersonalDraft:
        value = self.db.get(PersonalDraft, draft_id)
        if value is None or value.user_id != USER_ID or value.kind != kind:
            raise NotFoundError("预览不存在。")
        if value.consumed:
            raise ConflictError("该预览已执行，请勿重复提交。")
        if value.expires_at < datetime.now(UTC):
            raise ConflictError("预览已过期，请重新预览。")
        return value

    def commit_category(self, draft_id: str) -> dict:
        draft = self.get_draft(draft_id, "category")
        before = draft.payload["before"]
        target = self.category(draft.payload["target_id"])
        rows = [self.get_entry(item["id"]) for item in before]
        if [snapshot(row) for row in rows] != before:
            raise ConflictError("预览后账目已有变化，请重新预览。")
        for row in rows:
            row.category_id, row.needs_review, row.categorization_source = target.id, False, "user"
        self.db.flush()
        draft.consumed = True
        return {
            "count": len(rows),
            "action_id": self._audit("category", before, [snapshot(r) for r in rows]),
        }

    def history(self) -> list[dict]:
        return [
            {
                "id": x.id,
                "kind": x.kind,
                "created_at": x.created_at.isoformat(),
                "count": max(len(x.payload["before"]), len(x.payload["after"])),
                "undone": x.undone,
            }
            for x in self.db.scalars(
                select(PersonalAction)
                .where(PersonalAction.user_id == USER_ID)
                .order_by(PersonalAction.created_at.desc())
                .limit(100)
            )
        ]

    def undo(self, action_id: str) -> dict:
        action = self.db.get(PersonalAction, action_id)
        if action is None or action.user_id != USER_ID:
            raise NotFoundError("操作记录不存在。")
        if action.undone:
            raise ConflictError("该操作已经撤销。")
        before, after = action.payload["before"], action.payload["after"]
        for value in after:
            row = self.db.get(Transaction, value["id"])
            if row is None or snapshot(row) != value:
                raise ConflictError("相关账目后来已变动，请先撤销后续操作。")
        after_ids = {value["id"] for value in after}
        for value in before:
            if value["id"] not in after_ids and self.db.get(Transaction, value["id"]) is not None:
                raise ConflictError("原账目已被其他操作恢复。")
        for value in after:
            row = self.get_entry(value["id"])
            apply_delta(self.account(row.account_id), -row.amount_minor)
            self.db.delete(row)
        self.db.flush()
        for value in before:
            self.db.add(Transaction(**restore_values(value)))
            apply_delta(self.account(value["account_id"]), value["amount_minor"])
        self.db.flush()
        action.undone = True
        return {"undone": True, "action_id": action.id}

    def issue_token(self, writes: bool, details: bool) -> dict:
        token = secrets.token_urlsafe(32)
        scopes = ["analysis"] + (["write"] if writes else []) + (["details"] if details else [])
        self.set_setting(
            "agent", {"hash": hashlib.sha256(token.encode()).hexdigest(), "scopes": scopes}
        )
        return {"token": token, "scopes": scopes}

    def token_scopes(self, token: str) -> list[str]:
        value = self.setting("agent")
        if value is None or not secrets.compare_digest(
            value.value["hash"], hashlib.sha256(token.encode()).hexdigest()
        ):
            raise AuthError("Agent 令牌无效或已撤销。")
        return value.value["scopes"]
