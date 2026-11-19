"""Validated personal-edition API contracts. All amounts are integer fen/cents."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

MAX_AMOUNT = 9_000_000_000_000
type Kind = Literal["expense", "income", "transfer"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Unlock(Input):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    credential: str = Field(min_length=1, max_length=1024)
    recovery: bool = False


class Password(Input):
    # Preserve spaces in passwords; credentials are not merchant labels.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    password: str = Field(min_length=10, max_length=1024)


class AccountInput(Input):
    name: str = Field(min_length=1, max_length=100)
    opening_balance_minor: StrictInt = Field(default=0, ge=-MAX_AMOUNT, le=MAX_AMOUNT)
    archived: bool = False


class CategoryInput(Input):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["expense", "income"] = "expense"
    parent_id: str | None = None


class EntryInput(Input):
    account_id: str = Field(min_length=1, max_length=36)
    booked_date: date
    kind: Kind = "expense"
    amount_minor: StrictInt = Field(gt=0, le=MAX_AMOUNT)
    category_id: str | None = None
    merchant: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=2000)
    destination_account_id: str | None = None
    request_id: str = Field(min_length=8, max_length=100)

    @model_validator(mode="after")
    def coherent_transfer(self) -> EntryInput:
        if self.kind == "transfer":
            if not self.destination_account_id or self.destination_account_id == self.account_id:
                raise ValueError("转账需要两个不同的账户。")
            if self.category_id is not None:
                raise ValueError("转账不使用收入/支出分类。")
        elif self.destination_account_id is not None:
            raise ValueError("只有转账可以指定转入账户。")
        return self


class BulkCategory(Input):
    source_category_id: str
    target_category_id: str


class Confirm(Input):
    preview_id: str = Field(min_length=1, max_length=36)


class TokenInput(Input):
    allow_writes: bool = True
    allow_details: bool = False


class RuleInput(Input):
    keyword: str = Field(min_length=1, max_length=100)
    category_id: str
    merchant: str = Field(default="", max_length=200)


class ImportInput(Input):
    account_id: str
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1, max_length=4_000_000)
    format: Literal["auto", "wechat", "alipay", "csv", "mmex"] = "auto"


class RestoreInput(Input):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    # Backups are ciphertext, never an uploaded plaintext SQLite file.
    credential: str = Field(min_length=1, max_length=1024)
    recovery: bool = False
    content_base64: str = Field(min_length=1, max_length=180_000_000)
