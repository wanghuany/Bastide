"""Orchestration boundary: every operation uses the same encrypted vault and ledger."""

import base64
import binascii
import hashlib
from datetime import date
from typing import Any

from app.core.errors import ValidationError
from app.features.personal.analysis import run_report
from app.features.personal.data_repository import DataRepository
from app.features.personal.imports import parse_csv
from app.features.personal.repository import LedgerRepository
from app.features.personal.schemas import ImportInput
from app.features.personal.vault import Vault


class PersonalService:
    """One transaction/durable encrypted commit for each write, including import and undo."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    def invoke(self, method: str, *args: Any, write: bool = False, **kwargs: Any) -> Any:
        with self.vault.session(write=write) as db:
            return getattr(LedgerRepository(db), method)(*args, **kwargs)

    def bootstrap(self) -> None:
        self.invoke("bootstrap", write=True)

    def analysis(
        self,
        report: str,
        start: date,
        end: date,
        category_id: str | None = None,
        account_id: str | None = None,
    ) -> dict:
        with self.vault.session() as db:
            return run_report(LedgerRepository(db), report, start, end, category_id, account_id)

    def overview(self, start: date, end: date) -> dict:
        with self.vault.session() as db:
            repo = LedgerRepository(db)
            return {
                "summary": run_report(repo, "summary", start, end),
                "categories": repo.categories(),
                "recent": repo.entries(start=start, end=end, page_size=8)["items"],
            }

    def preview_import(self, data: ImportInput) -> dict:
        try:
            raw = base64.b64decode(data.content_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValidationError("文件编码无效，请重新选择 CSV 文件。") from exc
        parsed = parse_csv(raw, data.format)
        with self.vault.session(write=True) as db:
            return DataRepository(db).prepare_import(
                parsed, data.account_id, hashlib.sha256(raw).hexdigest()
            )

    def commit_import(self, preview_id: str) -> dict:
        with self.vault.session(write=True) as db:
            return DataRepository(db).commit_import(preview_id)

    def export(self) -> dict:
        """Explicit desktop-only plaintext export."""
        with self.vault.session() as db:
            return DataRepository(db).export()
