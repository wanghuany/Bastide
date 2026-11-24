"""Personal-mode audit history, expiring previews, and private settings."""

from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import JSON, BigInteger, Boolean, Date, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UtcDateTime


class PersonalAction(Base):
    """An atomic ledger change, with snapshots for conflict-checked undo."""

    __tablename__ = "personal_actions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    undone: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=lambda: datetime.now(UTC))


class PersonalDraft(Base):
    """A bounded, expiring import or category-preview snapshot, encrypted with the ledger."""

    __tablename__ = "personal_drafts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class PersonalSetting(Base):
    """Namespaced key/value storage; includes only token hashes, never bearer tokens."""

    __tablename__ = "personal_settings"
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    value: Mapped[dict] = mapped_column(JSON)


class PersonalHolding(Base):
    """A user's manually valued fund position; amounts are CNY minor units."""

    __tablename__ = "personal_holdings"
    __table_args__ = (UniqueConstraint("account_id", "name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    opening_date: Mapped[date] = mapped_column(Date())
    opening_value_minor: Mapped[int] = mapped_column(BigInteger())
    current_value_minor: Mapped[int] = mapped_column(BigInteger())


class PersonalHoldingEvent(Base):
    """Chronological value observations and cash-backed buys/sells, with reversible history."""

    __tablename__ = "personal_holding_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    holding_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("personal_holdings.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))
    booked_date: Mapped[date] = mapped_column(Date())
    previous_value_minor: Mapped[int] = mapped_column(BigInteger())
    value_minor: Mapped[int] = mapped_column(BigInteger())
    gain_minor: Mapped[int] = mapped_column(BigInteger())
    cash_amount_minor: Mapped[int] = mapped_column(BigInteger(), default=0)
    transaction_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("transactions.id"))
    undone: Mapped[bool] = mapped_column(Boolean(), default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=lambda: datetime.now(UTC))
