"""Persistence for import previews, deduplication and explicit exports."""

from datetime import date
from uuid import uuid4

from sqlalchemy import select

from app.features.accounts.models import Account
from app.features.imports.canonical import clean_description, compute_dedup_hash
from app.features.personal.repository import USER_ID, LedgerRepository, snapshot
from app.features.personal.schemas import EntryInput
from app.features.transactions.models import Transaction


class DataRepository(LedgerRepository):
    """Import/export queries sharing the same transaction and user scope as the ledger."""

    def prepare_import(self, parsed: dict, account_id: str, file_hash: str) -> dict:
        repo = self
        db = self.db
        repo.account(account_id, active=True)
        prepared, duplicates = [], 0
        seen = set()
        for row in parsed["records"]:
            signed = -row["amount_minor"] if row["kind"] == "expense" else row["amount_minor"]
            description = clean_description(row["merchant"] or row["note"] or "导入账目")
            dedup = compute_dedup_hash(
                account_id, date.fromisoformat(row["booked_date"]), signed, description
            )
            external = row["external_id"]
            key = external or dedup
            query = (
                select(Transaction.id)
                .join(Account)
                .where(Account.user_id == USER_ID, Transaction.account_id == account_id)
            )
            query = (
                query.where(Transaction.fitid == external)
                if external
                else query.where(Transaction.dedup_hash == dedup)
            )
            if key in seen or db.scalar(query.limit(1)) is not None:
                duplicates += 1
                continue
            seen.add(key)
            category_id, merchant = repo.classify(row["merchant"], row["note"], row["kind"])
            # An exact MMEX/template category label takes precedence over keyword suggestions.
            if row["original_category"]:
                matching = next(
                    (
                        c
                        for c in repo.categories()
                        if c["name"] == row["original_category"] and c["kind"] == row["kind"]
                    ),
                    None,
                )
                if matching:
                    category_id = matching["id"]
            prepared.append(
                {**row, "merchant": merchant, "category_id": category_id, "dedup_hash": dedup}
            )
        payload = {
            "account_id": account_id,
            "rows": prepared,
            "file_hash": file_hash,
            "format": parsed["format"],
        }
        draft = repo.draft("import", payload)
        return {
            "preview_id": draft.id,
            "count": len(prepared),
            "duplicates": duplicates,
            "skipped_count": len(parsed["skipped"]),
            "skipped": parsed["skipped"][:100],
            "items": prepared[:100],
            "format": parsed["format"],
            "unclassified": sum(x["category_id"] is None for x in prepared),
        }

    def commit_import(self, preview_id: str) -> dict:
        repo = self
        db = self.db
        draft = repo.get_draft(preview_id, "import")
        account = repo.account(draft.payload["account_id"], active=True)
        inserted, duplicates = [], 0
        for row in draft.payload["rows"]:
            query = select(Transaction.id).where(Transaction.account_id == account.id)
            query = (
                query.where(Transaction.fitid == row["external_id"])
                if row["external_id"]
                else query.where(Transaction.dedup_hash == row["dedup_hash"])
            )
            if db.scalar(query.limit(1)) is not None:
                duplicates += 1
                continue
            data = EntryInput(
                account_id=account.id,
                booked_date=row["booked_date"],
                kind=row["kind"],
                amount_minor=row["amount_minor"],
                category_id=row["category_id"],
                merchant=row["merchant"],
                note=row["note"],
                request_id=str(uuid4()),
            )
            new_row = repo._insert(data)[0]
            new_row.fitid = row["external_id"]
            new_row.dedup_hash = row["dedup_hash"]
            inserted.append(new_row)
        db.flush()
        draft.consumed = True
        action = repo._audit("import", [], [snapshot(row) for row in inserted])
        return {"count": len(inserted), "duplicates": duplicates, "action_id": action}

    def export(self) -> dict:
        repo = self
        db = self.db
        rows = db.scalars(
            select(Transaction)
            .join(Account)
            .where(Account.user_id == USER_ID)
            .order_by(Transaction.booked_date, Transaction.created_at)
        )
        return {
            "format": "bastide-personal-export-v1",
            "currency": "CNY",
            "accounts": repo.accounts(),
            "categories": repo.categories(),
            "transactions": [snapshot(row) for row in rows],
        }
