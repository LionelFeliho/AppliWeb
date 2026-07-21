from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import Database
from ..models import IngestionRun, MarketDataSnapshot, MarketQuote, utc_now
from .conversions import normalize_quote
from .domain import MarketDataConflictError, MarketDataValidationError, RawQuote


@dataclass(frozen=True)
class IngestionResult:
    run_id: str
    snapshot_id: str
    valuation_date: date
    provider: str
    status: str
    rows_received: int
    rows_inserted: int
    rows_updated: int
    rows_unchanged: int
    quote_count: int
    checksum: str
    warnings: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "valuation_date": self.valuation_date,
            "provider": self.provider,
            "status": self.status,
            "rows_received": self.rows_received,
            "rows_inserted": self.rows_inserted,
            "rows_updated": self.rows_updated,
            "rows_unchanged": self.rows_unchanged,
            "quote_count": self.quote_count,
            "checksum": self.checksum,
            "warnings": self.warnings,
        }


class MarketDataService:
    def __init__(self, database: Database):
        self.database = database

    def ingest_quotes(
        self,
        provider: str,
        valuation_date: date,
        quotes: Iterable[RawQuote],
        replace: bool = False,
    ) -> IngestionResult:
        quote_list = list(quotes)
        if not quote_list:
            raise MarketDataValidationError("Provider returned no quotes")

        run_id = self._start_run(provider, valuation_date, len(quote_list))
        try:
            result = self._apply_quotes(
                run_id=run_id,
                provider=provider,
                valuation_date=valuation_date,
                quotes=quote_list,
                replace=replace,
            )
            return result
        except Exception as exc:
            self._fail_run(run_id, str(exc))
            raise

    def _start_run(self, provider: str, valuation_date: date, rows_received: int) -> str:
        with self.database.session() as session:
            run = IngestionRun(
                valuation_date=valuation_date,
                provider=provider,
                status="RUNNING",
                rows_received=rows_received,
            )
            session.add(run)
            session.commit()
            return run.id

    def _fail_run(self, run_id: str, message: str) -> None:
        with self.database.session() as session:
            run = session.get(IngestionRun, run_id)
            if run is None:
                return
            run.status = "FAILED"
            run.error_message = message[:4000]
            run.completed_at = utc_now()
            session.commit()

    def _apply_quotes(
        self,
        run_id: str,
        provider: str,
        valuation_date: date,
        quotes: list[RawQuote],
        replace: bool,
    ) -> IngestionResult:
        with self.database.session() as session:
            snapshot = session.scalar(
                select(MarketDataSnapshot).where(
                    MarketDataSnapshot.valuation_date == valuation_date
                )
            )
            if snapshot is None:
                snapshot = MarketDataSnapshot(valuation_date=valuation_date, status="BUILDING")
                session.add(snapshot)
                session.flush()

            existing_quotes = {
                quote.canonical_id: quote
                for quote in session.scalars(
                    select(MarketQuote).where(MarketQuote.snapshot_id == snapshot.id)
                ).all()
            }

            inserted = 0
            updated = 0
            unchanged = 0
            warnings: list[str] = []
            seen_ids: set[str] = set()

            for raw_quote in quotes:
                if raw_quote.valuation_date != valuation_date:
                    raise MarketDataValidationError(
                        f"Quote {raw_quote.canonical_id} has date {raw_quote.valuation_date}, "
                        f"expected {valuation_date}"
                    )
                if raw_quote.canonical_id in seen_ids:
                    raise MarketDataValidationError(
                        f"Duplicate canonical_id in provider batch: {raw_quote.canonical_id}"
                    )
                seen_ids.add(raw_quote.canonical_id)

                normalized = normalize_quote(raw_quote)
                existing = existing_quotes.get(raw_quote.canonical_id)
                if existing is None:
                    session.add(self._new_quote(snapshot.id, raw_quote, normalized))
                    inserted += 1
                    continue

                if self._same_quote(existing, raw_quote, normalized):
                    unchanged += 1
                    continue

                if not replace:
                    raise MarketDataConflictError(
                        f"Quote {raw_quote.canonical_id} already exists with a different value/source. "
                        "Re-run with replace=true after reviewing the change."
                    )

                self._update_quote(existing, raw_quote, normalized)
                updated += 1

            session.flush()
            quote_count = session.scalar(
                select(func.count(MarketQuote.id)).where(MarketQuote.snapshot_id == snapshot.id)
            ) or 0
            checksum = self._checksum(session, snapshot.id)
            snapshot.quote_count = int(quote_count)
            snapshot.checksum = checksum
            snapshot.status = "READY"
            snapshot.updated_at = utc_now()

            run = session.get(IngestionRun, run_id)
            if run is None:  # pragma: no cover - defensive integrity check
                raise RuntimeError(f"Ingestion run disappeared: {run_id}")
            run.status = "SUCCEEDED"
            run.rows_inserted = inserted
            run.rows_updated = updated
            run.rows_unchanged = unchanged
            run.warnings = warnings
            run.completed_at = utc_now()
            session.commit()

            return IngestionResult(
                run_id=run.id,
                snapshot_id=snapshot.id,
                valuation_date=valuation_date,
                provider=provider,
                status=run.status,
                rows_received=len(quotes),
                rows_inserted=inserted,
                rows_updated=updated,
                rows_unchanged=unchanged,
                quote_count=int(quote_count),
                checksum=checksum,
                warnings=warnings,
            )

    @staticmethod
    def _new_quote(snapshot_id: str, raw: RawQuote, normalized: Decimal) -> MarketQuote:
        return MarketQuote(
            snapshot_id=snapshot_id,
            canonical_id=raw.canonical_id,
            asset_class=raw.asset_class.value,
            quote_type=raw.quote_type.value,
            currency=raw.currency,
            unit=raw.unit,
            raw_value=raw.raw_value,
            normalized_value=normalized,
            conversion=raw.conversion.value,
            factor=raw.factor,
            shift=raw.shift,
            source=raw.source,
            source_symbol=raw.source_symbol,
            quote_metadata=raw.metadata,
        )

    @staticmethod
    def _same_quote(existing: MarketQuote, raw: RawQuote, normalized: Decimal) -> bool:
        return (
            existing.normalized_value == normalized
            and existing.raw_value == raw.raw_value
            and existing.source == raw.source
            and existing.source_symbol == raw.source_symbol
            and existing.conversion == raw.conversion.value
            and existing.factor == raw.factor
            and existing.shift == raw.shift
            and existing.asset_class == raw.asset_class.value
            and existing.quote_type == raw.quote_type.value
            and existing.currency == raw.currency
            and existing.unit == raw.unit
            and existing.quote_metadata == raw.metadata
        )

    @staticmethod
    def _update_quote(existing: MarketQuote, raw: RawQuote, normalized: Decimal) -> None:
        existing.asset_class = raw.asset_class.value
        existing.quote_type = raw.quote_type.value
        existing.currency = raw.currency
        existing.unit = raw.unit
        existing.raw_value = raw.raw_value
        existing.normalized_value = normalized
        existing.conversion = raw.conversion.value
        existing.factor = raw.factor
        existing.shift = raw.shift
        existing.source = raw.source
        existing.source_symbol = raw.source_symbol
        existing.quote_metadata = raw.metadata
        existing.updated_at = utc_now()

    @staticmethod
    def _checksum(session: Session, snapshot_id: str) -> str:
        rows = session.scalars(
            select(MarketQuote)
            .where(MarketQuote.snapshot_id == snapshot_id)
            .order_by(MarketQuote.canonical_id)
        ).all()
        payload = [
            {
                "canonical_id": row.canonical_id,
                "asset_class": row.asset_class,
                "quote_type": row.quote_type,
                "currency": row.currency,
                "unit": row.unit,
                "raw_value": format(row.raw_value, "f"),
                "normalized_value": format(row.normalized_value, "f"),
                "conversion": row.conversion,
                "factor": format(row.factor, "f"),
                "shift": format(row.shift, "f"),
                "source": row.source,
                "source_symbol": row.source_symbol,
                "metadata": row.quote_metadata,
            }
            for row in rows
        ]
        canonical_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def resolve_snapshot(
        self, as_of_date: date, policy: str = "exact"
    ) -> dict[str, Any] | None:
        """Resolve a READY snapshot without ever looking beyond the requested date."""
        if policy not in {"exact", "previous_ready"}:
            raise ValueError(f"Unsupported snapshot policy: {policy}")

        with self.database.session() as session:
            statement = select(MarketDataSnapshot).where(
                MarketDataSnapshot.status == "READY"
            )
            if policy == "exact":
                statement = statement.where(
                    MarketDataSnapshot.valuation_date == as_of_date
                )
            else:
                statement = (
                    statement.where(MarketDataSnapshot.valuation_date <= as_of_date)
                    .order_by(MarketDataSnapshot.valuation_date.desc())
                    .limit(1)
                )

            snapshot = session.scalar(statement)
            if snapshot is None:
                return None

            market_data_date = snapshot.valuation_date
            return {
                "as_of_date": as_of_date,
                "market_data_date": market_data_date,
                "snapshot_policy": policy,
                "exact_snapshot": market_data_date == as_of_date,
                "snapshot": self._snapshot_dict(snapshot),
            }

    def list_snapshots(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.database.session() as session:
            snapshots = session.scalars(
                select(MarketDataSnapshot)
                .order_by(MarketDataSnapshot.valuation_date.desc())
                .limit(limit)
            ).all()
            return [self._snapshot_dict(item) for item in snapshots]

    def get_snapshot(self, valuation_date: date) -> dict[str, Any] | None:
        with self.database.session() as session:
            snapshot = session.scalar(
                select(MarketDataSnapshot).where(
                    MarketDataSnapshot.valuation_date == valuation_date
                )
            )
            return self._snapshot_dict(snapshot) if snapshot else None

    def get_quotes(self, valuation_date: date) -> list[dict[str, Any]] | None:
        with self.database.session() as session:
            snapshot = session.scalar(
                select(MarketDataSnapshot).where(
                    MarketDataSnapshot.valuation_date == valuation_date
                )
            )
            if snapshot is None:
                return None
            quotes = session.scalars(
                select(MarketQuote)
                .where(MarketQuote.snapshot_id == snapshot.id)
                .order_by(MarketQuote.asset_class, MarketQuote.canonical_id)
            ).all()
            return [self._quote_dict(quote, valuation_date) for quote in quotes]

    def get_quote(self, valuation_date: date, canonical_id: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            snapshot = session.scalar(
                select(MarketDataSnapshot).where(
                    MarketDataSnapshot.valuation_date == valuation_date
                )
            )
            if snapshot is None:
                return None
            quote = session.scalar(
                select(MarketQuote).where(
                    MarketQuote.snapshot_id == snapshot.id,
                    MarketQuote.canonical_id == canonical_id,
                )
            )
            return self._quote_dict(quote, valuation_date) if quote else None

    @staticmethod
    def _snapshot_dict(snapshot: MarketDataSnapshot) -> dict[str, Any]:
        return {
            "id": snapshot.id,
            "valuation_date": snapshot.valuation_date,
            "status": snapshot.status,
            "quote_count": snapshot.quote_count,
            "checksum": snapshot.checksum,
            "created_at": snapshot.created_at,
            "updated_at": snapshot.updated_at,
        }

    @staticmethod
    def _quote_dict(quote: MarketQuote, valuation_date: date) -> dict[str, Any]:
        return {
            "id": quote.id,
            "valuation_date": valuation_date,
            "canonical_id": quote.canonical_id,
            "asset_class": quote.asset_class,
            "quote_type": quote.quote_type,
            "currency": quote.currency,
            "unit": quote.unit,
            "raw_value": quote.raw_value,
            "normalized_value": quote.normalized_value,
            "conversion": quote.conversion,
            "factor": quote.factor,
            "shift": quote.shift,
            "source": quote.source,
            "source_symbol": quote.source_symbol,
            "metadata": quote.quote_metadata,
            "created_at": quote.created_at,
            "updated_at": quote.updated_at,
        }
