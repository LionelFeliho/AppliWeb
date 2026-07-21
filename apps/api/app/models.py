from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class MarketDataSnapshot(Base):
    __tablename__ = "market_data_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    valuation_date: Mapped[date] = mapped_column(Date, unique=True, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="BUILDING", nullable=False)
    quote_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    quotes: Mapped[list["MarketQuote"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class MarketQuote(Base):
    __tablename__ = "market_quotes"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "canonical_id", name="uq_snapshot_canonical_quote"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("market_data_snapshots.id", ondelete="CASCADE"), index=True, nullable=False
    )
    canonical_id: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False)
    quote_type: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(12), nullable=True)
    unit: Mapped[str] = mapped_column(String(48), nullable=False)
    raw_value: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    normalized_value: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    conversion: Mapped[str] = mapped_column(String(40), nullable=False)
    factor: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=Decimal("1"), nullable=False)
    shift: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=Decimal("0"), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    source_symbol: Mapped[str] = mapped_column(String(160), nullable=False)
    quote_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    snapshot: Mapped[MarketDataSnapshot] = relationship(back_populates="quotes")


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    valuation_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="RUNNING", nullable=False)
    rows_received: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_unchanged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
