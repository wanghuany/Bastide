"""Personal-mode audit history, expiring previews, and private settings."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, ForeignKey, String
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
