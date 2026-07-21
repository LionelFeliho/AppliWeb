from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from .curves import DiscountCurve, build_ois_curve, curve_completeness


_FX_RE = re.compile(r"^FX\.([A-Z]{3})([A-Z]{3})\.SPOT$")
_VOL_RE = re.compile(r"^VOL\.FX\.([A-Z]{3})([A-Z]{3})\.ATM\.(?:1Y|12M)$")
_BASIS_RE = re.compile(r"^BASIS\.([A-Z]{3})([A-Z]{3})\.XCCY(?:\..+)?$")


@dataclass(frozen=True)
class ResolvedValue:
    value: float
    source: str
    path: tuple[str, ...]
    warning: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "source": self.source,
            "path": list(self.path),
            "warning": self.warning,
        }


class CanonicalMarket:
    def __init__(self, as_of_date: date, quotes: Iterable[dict[str, Any]]):
        self.as_of_date = as_of_date
        self.quotes = tuple(quotes)
        self.by_id = {str(quote["canonical_id"]).upper(): quote for quote in self.quotes}
        self._curves: dict[str, DiscountCurve] = {}

    def curve(self, currency: str) -> DiscountCurve:
        code = currency.upper()
        if code not in self._curves:
            self._curves[code] = build_ois_curve(self.as_of_date, self.quotes, code)
        return self._curves[code]

    def curve_readiness(self, currency: str) -> dict[str, object]:
        return curve_completeness(self.quotes, currency)

    def _fx_graph(self) -> dict[str, list[tuple[str, float, str]]]:
        graph: dict[str, list[tuple[str, float, str]]] = {}
        for canonical_id, quote in self.by_id.items():
            match = _FX_RE.fullmatch(canonical_id)
            if match is None or quote.get("quote_type") != "SPOT":
                continue
            base, quote_currency = match.groups()
            value = float(quote["normalized_value"])
            if value <= 0:
                continue
            graph.setdefault(base, []).append((quote_currency, value, canonical_id))
            graph.setdefault(quote_currency, []).append((base, 1.0 / value, canonical_id))
        return graph

    def spot(self, base_currency: str, quote_currency: str) -> ResolvedValue:
        base = base_currency.upper()
        quote = quote_currency.upper()
        if base == quote:
            return ResolvedValue(1.0, "IDENTITY", (base,))

        graph = self._fx_graph()
        queue: deque[tuple[str, float, tuple[str, ...], tuple[str, ...]]] = deque(
            [(base, 1.0, (base,), ())]
        )
        visited = {base}
        while queue:
            currency, accumulated, path, sources = queue.popleft()
            for target, rate, source_id in graph.get(currency, []):
                if target in visited:
                    continue
                next_value = accumulated * rate
                next_path = path + (target,)
                next_sources = sources + (source_id,)
                if target == quote:
                    source = " -> ".join(next_sources)
                    warning = None if len(next_sources) == 1 else "FX spot triangulated"
                    return ResolvedValue(next_value, source, next_path, warning)
                visited.add(target)
                queue.append((target, next_value, next_path, next_sources))
        raise ValueError(f"No FX spot path available for {base}/{quote}")

    def _direct_vol(self, base: str, quote: str) -> ResolvedValue | None:
        direct = self.by_id.get(f"VOL.FX.{base}{quote}.ATM.1Y")
        if direct is not None:
            return ResolvedValue(
                float(direct["normalized_value"]),
                str(direct["canonical_id"]),
                (base, quote),
            )
        inverse = self.by_id.get(f"VOL.FX.{quote}{base}.ATM.1Y")
        if inverse is not None:
            return ResolvedValue(
                float(inverse["normalized_value"]),
                str(inverse["canonical_id"]),
                (base, quote),
            )
        return None

    def fx_volatility(
        self,
        base_currency: str,
        quote_currency: str,
        override: float | None = None,
    ) -> ResolvedValue:
        base = base_currency.upper()
        quote = quote_currency.upper()
        if override is not None:
            if override <= 0:
                raise ValueError("FX volatility override must be positive")
            return ResolvedValue(override, "REQUEST_OVERRIDE", (base, quote))

        direct = self._direct_vol(base, quote)
        if direct is not None:
            return direct

        if base != "USD" and quote != "USD":
            left = self._direct_vol(base, "USD")
            right = self._direct_vol(quote, "USD")
            if left is not None and right is not None:
                assumed_correlation = 0.25
                variance = (
                    left.value * left.value
                    + right.value * right.value
                    - 2.0 * assumed_correlation * left.value * right.value
                )
                return ResolvedValue(
                    math.sqrt(max(variance, 1e-10)),
                    f"TRIANGULATED({left.source},{right.source})",
                    (base, "USD", quote),
                    "Cross volatility uses a 25% correlation assumption",
                )
        raise ValueError(
            f"No ATM 1Y FX volatility available for {base}/{quote}; provide an override"
        )

    def _direct_basis(self, base: str, quote: str) -> ResolvedValue | None:
        prefix = f"BASIS.{base}{quote}.XCCY"
        candidates = [
            (canonical_id, row)
            for canonical_id, row in self.by_id.items()
            if canonical_id == prefix or canonical_id.startswith(prefix + ".")
        ]
        if candidates:
            canonical_id, row = sorted(candidates, key=lambda item: item[0])[0]
            return ResolvedValue(float(row["normalized_value"]), canonical_id, (base, quote))

        inverse_prefix = f"BASIS.{quote}{base}.XCCY"
        inverse_candidates = [
            (canonical_id, row)
            for canonical_id, row in self.by_id.items()
            if canonical_id == inverse_prefix or canonical_id.startswith(inverse_prefix + ".")
        ]
        if inverse_candidates:
            canonical_id, row = sorted(inverse_candidates, key=lambda item: item[0])[0]
            return ResolvedValue(-float(row["normalized_value"]), canonical_id, (base, quote))
        return None

    def xccy_basis(
        self,
        base_currency: str,
        quote_currency: str,
        override_bps: float | None = None,
    ) -> ResolvedValue:
        base = base_currency.upper()
        quote = quote_currency.upper()
        if override_bps is not None:
            return ResolvedValue(
                override_bps / 10_000.0, "REQUEST_OVERRIDE", (base, quote)
            )
        direct = self._direct_basis(base, quote)
        if direct is not None:
            return direct
        if base != "USD" and quote != "USD":
            left = self._direct_basis(base, "USD")
            right = self._direct_basis(quote, "USD")
            if left is not None and right is not None:
                return ResolvedValue(
                    left.value - right.value,
                    f"TRIANGULATED({left.source},{right.source})",
                    (base, "USD", quote),
                    "Cross-currency basis triangulated through USD",
                )
        return ResolvedValue(
            0.0,
            "ZERO_FALLBACK",
            (base, quote),
            "No XCCY basis quote found; zero basis used explicitly",
        )

    def readiness(self, base_currency: str, quote_currency: str) -> dict[str, object]:
        base_curve = self.curve_readiness(base_currency)
        quote_curve = self.curve_readiness(quote_currency)
        missing: list[str] = []
        warnings: list[str] = []
        try:
            spot = self.spot(base_currency, quote_currency)
            if spot.warning:
                warnings.append(spot.warning)
        except ValueError as exc:
            spot = None
            missing.append(str(exc))
        try:
            volatility = self.fx_volatility(base_currency, quote_currency)
            if volatility.warning:
                warnings.append(volatility.warning)
        except ValueError as exc:
            volatility = None
            missing.append(str(exc))
        basis = self.xccy_basis(base_currency, quote_currency)
        if basis.warning:
            warnings.append(basis.warning)
        for curve_result in (base_curve, quote_curve):
            if not curve_result["ready"]:
                missing.append(
                    f"{curve_result['currency']} OIS missing core tenors: "
                    + ", ".join(curve_result["missing_core_tenors"])
                )
        return {
            "base_currency": base_currency.upper(),
            "quote_currency": quote_currency.upper(),
            "ready": not missing,
            "missing": missing,
            "warnings": warnings,
            "spot": spot.as_dict() if spot else None,
            "volatility": volatility.as_dict() if volatility else None,
            "basis": basis.as_dict(),
            "base_curve": base_curve,
            "quote_curve": quote_curve,
        }
