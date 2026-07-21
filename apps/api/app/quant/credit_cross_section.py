from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Iterable, Literal, Mapping, Sequence

import numpy as np

from .conventions import parse_tenor_years


DIMENSIONS: tuple[str, ...] = ("sector", "region", "rating", "seniority")
AggregationMethod = Literal["arithmetic", "geometric"]
MissingCategoryPolicy = Literal["error", "global"]


@dataclass(frozen=True)
class CreditObservation:
    entity_id: str
    tenor: str
    time: float
    spread: float
    sector: str
    region: str
    rating: str
    seniority: str
    weight: float
    quote_id: str

    @property
    def attributes(self) -> dict[str, str]:
        return {
            "sector": self.sector,
            "region": self.region,
            "rating": self.rating,
            "seniority": self.seniority,
        }


@dataclass(frozen=True)
class BasketConstituent:
    sector: str
    region: str
    rating: str
    seniority: str
    weight: float = 1.0
    name: str | None = None

    @property
    def attributes(self) -> dict[str, str]:
        return {
            "sector": normalize_category(self.sector),
            "region": normalize_category(self.region),
            "rating": normalize_rating(self.rating),
            "seniority": normalize_category(self.seniority),
        }


@dataclass(frozen=True)
class TenorFit:
    tenor: str
    time: float
    global_log_spread: float
    factors: dict[str, dict[str, float]]
    category_counts: dict[str, dict[str, int]]
    category_weights: dict[str, dict[str, float]]
    observations: int
    effective_weight: float
    rank: int
    parameters: int
    condition_number: float
    log_rmse: float
    log_r_squared: float
    quote_ids: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def global_spread(self) -> float:
        return math.exp(self.global_log_spread)

    def proxy_spread(
        self,
        attributes: Mapping[str, str],
        missing_category_policy: MissingCategoryPolicy = "error",
    ) -> tuple[float, list[str]]:
        log_spread = self.global_log_spread
        warnings: list[str] = []
        for dimension in DIMENSIONS:
            value = attributes.get(dimension)
            normalized = normalize_rating(value) if dimension == "rating" else normalize_category(value)
            factor = self.factors[dimension].get(normalized)
            if factor is None:
                if missing_category_policy == "global":
                    warnings.append(
                        f"{self.tenor}: unseen {dimension}={normalized}; global factor 1.0 used"
                    )
                    continue
                known = ", ".join(sorted(self.factors[dimension]))
                raise ValueError(
                    f"Unseen {dimension}={normalized!r} at tenor {self.tenor}; known values: {known}"
                )
            log_spread += math.log(factor)
        return math.exp(log_spread), warnings

    def as_dict(self) -> dict[str, object]:
        return {
            "tenor": self.tenor,
            "time": self.time,
            "global_spread": self.global_spread,
            "global_spread_bps": self.global_spread * 10_000.0,
            "factors": self.factors,
            "category_counts": self.category_counts,
            "category_weights": self.category_weights,
            "diagnostics": {
                "observations": self.observations,
                "effective_weight": self.effective_weight,
                "rank": self.rank,
                "parameters": self.parameters,
                "condition_number": self.condition_number,
                "log_rmse": self.log_rmse,
                "log_r_squared": self.log_r_squared,
            },
            "quote_ids": list(self.quote_ids),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class CreditCurveNode:
    tenor: str
    time: float
    spread: float
    hazard_rate: float
    survival_probability: float
    cumulative_default_probability: float
    arithmetic_spread: float
    geometric_spread: float

    def as_dict(self) -> dict[str, float | str]:
        return {
            "tenor": self.tenor,
            "time": self.time,
            "spread": self.spread,
            "spread_bps": self.spread * 10_000.0,
            "arithmetic_spread": self.arithmetic_spread,
            "arithmetic_spread_bps": self.arithmetic_spread * 10_000.0,
            "geometric_spread": self.geometric_spread,
            "geometric_spread_bps": self.geometric_spread * 10_000.0,
            "hazard_rate": self.hazard_rate,
            "survival_probability": self.survival_probability,
            "cumulative_default_probability": self.cumulative_default_probability,
        }


class BasketCreditCurve:
    """Piecewise-constant hazard curve derived from cross-section proxy spreads.

    The hazard conversion deliberately uses the transparent first-order relation
    ``hazard = spread / (1 - recovery)``. This is suitable for proxy/XVA plumbing,
    but is not a replacement for a full CDS premium/protection-leg bootstrap.
    """

    def __init__(
        self,
        curve_id: str,
        recovery_rate: float,
        aggregation: AggregationMethod,
        nodes: Sequence[CreditCurveNode],
        warnings: Sequence[str] = (),
    ) -> None:
        if not 0.0 <= recovery_rate < 1.0:
            raise ValueError("recovery_rate must be in [0, 1)")
        if not nodes:
            raise ValueError("credit curve requires at least one node")
        ordered = tuple(sorted(nodes, key=lambda node: node.time))
        if any(node.time <= 0.0 for node in ordered):
            raise ValueError("credit curve node times must be positive")
        self.curve_id = curve_id
        self.recovery_rate = recovery_rate
        self.aggregation = aggregation
        self.nodes = ordered
        self.warnings = tuple(warnings)

    def hazard(self, time: float) -> float:
        if time <= self.nodes[0].time:
            return self.nodes[0].hazard_rate
        for node in self.nodes[1:]:
            if time <= node.time:
                return node.hazard_rate
        return self.nodes[-1].hazard_rate

    def survival(self, time: float) -> float:
        if time <= 0.0:
            return 1.0
        cumulative = 0.0
        previous = 0.0
        for node in self.nodes:
            end = min(time, node.time)
            if end > previous:
                cumulative += node.hazard_rate * (end - previous)
                previous = end
            if time <= node.time:
                return math.exp(-cumulative)
        if time > previous:
            cumulative += self.nodes[-1].hazard_rate * (time - previous)
        return math.exp(-cumulative)

    def default_probability(self, start_time: float, end_time: float) -> float:
        if end_time < start_time:
            raise ValueError("end_time must be on or after start_time")
        return max(self.survival(start_time) - self.survival(end_time), 0.0)

    def as_dict(self) -> dict[str, object]:
        return {
            "curve_id": self.curve_id,
            "model": "nomura_cross_section_log_ols",
            "hazard_conversion": "piecewise_spread_over_lgd",
            "recovery_rate": self.recovery_rate,
            "aggregation": self.aggregation,
            "nodes": [node.as_dict() for node in self.nodes],
            "warnings": list(self.warnings),
        }


def normalize_category(value: object) -> str:
    text = str(value or "").strip().upper()
    text = re.sub(r"[^A-Z0-9]+", "_", text).strip("_")
    if not text:
        raise ValueError("credit cross-section category cannot be empty")
    return text


def normalize_rating(value: object) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    aliases = {
        "AA-AAA": "AA_AAA",
        "AAA-AA": "AA_AAA",
        "NR": "NOT_RATED",
        "N/R": "NOT_RATED",
    }
    if text in aliases:
        return aliases[text]
    normalized = re.sub(r"[^A-Z0-9+_-]+", "_", text).strip("_")
    if not normalized:
        raise ValueError("credit rating cannot be empty")
    return normalized


def normalize_tenor(value: object) -> str:
    text = str(value or "").strip().upper()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[DWMY]", text):
        raise ValueError(f"Invalid credit tenor {value!r}")
    parse_tenor_years(text)
    return text


def _category_statistics(
    observations: Sequence[CreditObservation], dimension: str
) -> tuple[dict[str, int], dict[str, float]]:
    counts: dict[str, int] = {}
    weights: dict[str, float] = {}
    for observation in observations:
        category = observation.attributes[dimension]
        counts[category] = counts.get(category, 0) + 1
        weights[category] = weights.get(category, 0.0) + observation.weight
    return counts, weights


def fit_tenor_cross_section(
    observations: Sequence[CreditObservation],
    *,
    ridge: float = 1e-8,
    minimum_observations: int = 8,
) -> TenorFit:
    if not observations:
        raise ValueError("No credit observations supplied")
    tenor = observations[0].tenor
    if any(observation.tenor != tenor for observation in observations):
        raise ValueError("fit_tenor_cross_section accepts one tenor at a time")
    if len(observations) < minimum_observations:
        raise ValueError(
            f"Tenor {tenor} has {len(observations)} observations; "
            f"minimum is {minimum_observations}"
        )
    if ridge < 0.0:
        raise ValueError("ridge must be non-negative")

    category_counts: dict[str, dict[str, int]] = {}
    category_weights: dict[str, dict[str, float]] = {}
    baselines: dict[str, str] = {}
    columns: list[tuple[str, str]] = []
    for dimension in DIMENSIONS:
        counts, weights = _category_statistics(observations, dimension)
        category_counts[dimension] = counts
        category_weights[dimension] = weights
        baseline = sorted(weights, key=lambda category: (-weights[category], category))[0]
        baselines[dimension] = baseline
        columns.extend(
            (dimension, category)
            for category in sorted(weights)
            if category != baseline
        )

    x = np.zeros((len(observations), 1 + len(columns)), dtype=float)
    x[:, 0] = 1.0
    y = np.array([math.log(observation.spread) for observation in observations], dtype=float)
    weights = np.array([observation.weight for observation in observations], dtype=float)
    if np.any(weights <= 0.0) or not np.all(np.isfinite(weights)):
        raise ValueError("observation weights must be positive and finite")

    for row_index, observation in enumerate(observations):
        attributes = observation.attributes
        for column_index, (dimension, category) in enumerate(columns, start=1):
            if attributes[dimension] == category:
                x[row_index, column_index] = 1.0

    sqrt_weight = np.sqrt(weights)
    weighted_x = x * sqrt_weight[:, None]
    weighted_y = y * sqrt_weight
    design_rank = int(np.linalg.matrix_rank(weighted_x))
    design_singular_values = np.linalg.svd(weighted_x, compute_uv=False)
    if ridge > 0.0 and x.shape[1] > 1:
        penalty = np.zeros((x.shape[1] - 1, x.shape[1]), dtype=float)
        penalty[:, 1:] = np.eye(x.shape[1] - 1) * math.sqrt(ridge)
        weighted_x = np.vstack([weighted_x, penalty])
        weighted_y = np.concatenate([weighted_y, np.zeros(x.shape[1] - 1)])

    coefficients, _, _, _ = np.linalg.lstsq(weighted_x, weighted_y, rcond=None)
    intercept = float(coefficients[0])
    effects: dict[str, dict[str, float]] = {
        dimension: {category: 0.0 for category in category_counts[dimension]}
        for dimension in DIMENSIONS
    }
    for coefficient, (dimension, category) in zip(coefficients[1:], columns):
        effects[dimension][category] = float(coefficient)

    for dimension in DIMENSIONS:
        dimension_weights = category_weights[dimension]
        total_weight = sum(dimension_weights.values())
        mean_effect = sum(
            dimension_weights[category] * effects[dimension][category]
            for category in effects[dimension]
        ) / total_weight
        for category in effects[dimension]:
            effects[dimension][category] -= mean_effect
        intercept += mean_effect

    fitted = np.full(len(observations), intercept, dtype=float)
    for dimension in DIMENSIONS:
        fitted += np.array(
            [effects[dimension][observation.attributes[dimension]] for observation in observations]
        )
    residuals = y - fitted
    weighted_mean = float(np.average(y, weights=weights))
    ss_res = float(np.sum(weights * residuals**2))
    ss_tot = float(np.sum(weights * (y - weighted_mean) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 1e-18 else 1.0
    rmse = math.sqrt(ss_res / float(np.sum(weights)))
    condition_number = (
        float(design_singular_values[0] / design_singular_values[-1])
        if len(design_singular_values) and design_singular_values[-1] > 0.0
        else float("inf")
    )

    warnings: list[str] = []
    if design_rank < x.shape[1]:
        warnings.append(
            f"Tenor {tenor} design matrix rank {design_rank} is below {x.shape[1]} parameters; "
            "ridge regularisation stabilised the fit"
        )
    if condition_number > 1e8:
        warnings.append(f"Tenor {tenor} regression is ill-conditioned ({condition_number:.3g})")
    for dimension in DIMENSIONS:
        thin = [
            category
            for category, count in category_counts[dimension].items()
            if count < 2
        ]
        if thin:
            warnings.append(
                f"Tenor {tenor} has thin {dimension} categories: {', '.join(sorted(thin))}"
            )

    return TenorFit(
        tenor=tenor,
        time=observations[0].time,
        global_log_spread=intercept,
        factors={
            dimension: {
                category: math.exp(effect)
                for category, effect in sorted(effects[dimension].items())
            }
            for dimension in DIMENSIONS
        },
        category_counts=category_counts,
        category_weights=category_weights,
        observations=len(observations),
        effective_weight=float(np.sum(weights)),
        rank=design_rank,
        parameters=int(x.shape[1]),
        condition_number=condition_number,
        log_rmse=rmse,
        log_r_squared=r_squared,
        quote_ids=tuple(sorted(observation.quote_id for observation in observations)),
        warnings=tuple(warnings),
    )


def fit_cross_section_curve(
    observations: Iterable[CreditObservation],
    *,
    ridge: float = 1e-8,
    minimum_observations: int = 8,
    requested_tenors: Sequence[str] | None = None,
) -> list[TenorFit]:
    grouped: dict[str, list[CreditObservation]] = {}
    for observation in observations:
        grouped.setdefault(observation.tenor, []).append(observation)
    if not grouped:
        raise ValueError("No eligible liquid credit observations found")

    if requested_tenors:
        tenors = [normalize_tenor(tenor) for tenor in requested_tenors]
        missing = [tenor for tenor in tenors if tenor not in grouped]
        if missing:
            raise ValueError(f"No eligible observations for tenors: {', '.join(missing)}")
    else:
        tenors = sorted(grouped, key=parse_tenor_years)

    return [
        fit_tenor_cross_section(
            grouped[tenor], ridge=ridge, minimum_observations=minimum_observations
        )
        for tenor in tenors
    ]


def build_basket_credit_curve(
    fits: Sequence[TenorFit],
    basket: Sequence[BasketConstituent],
    *,
    recovery_rate: float = 0.4,
    aggregation: AggregationMethod = "arithmetic",
    missing_category_policy: MissingCategoryPolicy = "error",
    snapshot_checksum: str = "",
) -> tuple[BasketCreditCurve, list[dict[str, object]]]:
    if not fits:
        raise ValueError("At least one cross-section tenor fit is required")
    if not basket:
        raise ValueError("Basket must contain at least one constituent")
    if not 0.0 <= recovery_rate < 1.0:
        raise ValueError("recovery_rate must be in [0, 1)")
    if aggregation not in {"arithmetic", "geometric"}:
        raise ValueError(f"Unsupported basket aggregation: {aggregation}")

    raw_weights = [constituent.weight for constituent in basket]
    if any(weight <= 0.0 or not math.isfinite(weight) for weight in raw_weights):
        raise ValueError("Basket weights must be positive and finite")
    total_weight = sum(raw_weights)
    weights = [weight / total_weight for weight in raw_weights]

    curve_warnings: list[str] = [
        "Hazards use the transparent spread/(1-recovery) approximation, not a full CDS bootstrap."
    ]
    node_payloads: list[dict[str, object]] = []
    nodes: list[CreditCurveNode] = []
    cumulative_hazard = 0.0
    previous_time = 0.0

    for fit in sorted(fits, key=lambda item: item.time):
        constituent_spreads: list[float] = []
        constituent_payload: list[dict[str, object]] = []
        for constituent, weight in zip(basket, weights):
            spread, warnings = fit.proxy_spread(
                constituent.attributes, missing_category_policy=missing_category_policy
            )
            curve_warnings.extend(warnings)
            constituent_spreads.append(spread)
            constituent_payload.append(
                {
                    "name": constituent.name,
                    "weight": weight,
                    **constituent.attributes,
                    "proxy_spread": spread,
                    "proxy_spread_bps": spread * 10_000.0,
                }
            )

        arithmetic_spread = sum(
            weight * spread for weight, spread in zip(weights, constituent_spreads)
        )
        geometric_spread = math.exp(
            sum(weight * math.log(spread) for weight, spread in zip(weights, constituent_spreads))
        )
        selected_spread = arithmetic_spread if aggregation == "arithmetic" else geometric_spread
        hazard_rate = selected_spread / max(1.0 - recovery_rate, 1e-12)
        cumulative_hazard += hazard_rate * (fit.time - previous_time)
        survival = math.exp(-cumulative_hazard)
        node = CreditCurveNode(
            tenor=fit.tenor,
            time=fit.time,
            spread=selected_spread,
            hazard_rate=hazard_rate,
            survival_probability=survival,
            cumulative_default_probability=1.0 - survival,
            arithmetic_spread=arithmetic_spread,
            geometric_spread=geometric_spread,
        )
        nodes.append(node)
        node_payloads.append({**node.as_dict(), "constituents": constituent_payload})
        previous_time = fit.time

    identity_payload = {
        "snapshot_checksum": snapshot_checksum,
        "recovery_rate": recovery_rate,
        "aggregation": aggregation,
        "basket": [
            {**constituent.attributes, "weight": weight, "name": constituent.name}
            for constituent, weight in zip(basket, weights)
        ],
        "tenors": [fit.tenor for fit in fits],
    }
    digest = hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    curve = BasketCreditCurve(
        curve_id=f"CR.XS.NOMURA.{digest}",
        recovery_rate=recovery_rate,
        aggregation=aggregation,
        nodes=nodes,
        warnings=tuple(dict.fromkeys(curve_warnings)),
    )
    return curve, node_payloads


def calculate_credit_xva(
    exposure_profile: Sequence[Mapping[str, float]],
    counterparty_curve: BasketCreditCurve,
    *,
    own_curve: BasketCreditCurve | None = None,
    discount_rate: float = 0.0,
) -> dict[str, object]:
    if not exposure_profile:
        raise ValueError("exposure_profile cannot be empty")
    ordered = sorted(exposure_profile, key=lambda point: float(point["time"]))
    if float(ordered[0]["time"]) > 1e-12:
        ordered = [{"time": 0.0, "epe": 0.0, "ene": 0.0}, *ordered]

    cva = 0.0
    dva = 0.0
    intervals: list[dict[str, float]] = []
    for previous, current in zip(ordered[:-1], ordered[1:]):
        start = float(previous["time"])
        end = float(current["time"])
        if end <= start:
            raise ValueError("exposure profile times must be strictly increasing")
        midpoint = 0.5 * (start + end)
        discount = math.exp(-discount_rate * midpoint)
        epe = 0.5 * (max(float(previous.get("epe", 0.0)), 0.0) + max(float(current.get("epe", 0.0)), 0.0))
        previous_ene = abs(min(float(previous.get("ene", 0.0)), 0.0))
        current_ene = abs(min(float(current.get("ene", 0.0)), 0.0))
        ene = 0.5 * (previous_ene + current_ene)
        cp_default_probability = counterparty_curve.default_probability(start, end)
        own_default_probability = own_curve.default_probability(start, end) if own_curve else 0.0
        cva_contribution = (
            (1.0 - counterparty_curve.recovery_rate)
            * epe
            * cp_default_probability
            * discount
        )
        dva_contribution = (
            (1.0 - own_curve.recovery_rate)
            * ene
            * own_default_probability
            * discount
            if own_curve
            else 0.0
        )
        cva += cva_contribution
        dva += dva_contribution
        intervals.append(
            {
                "start_time": start,
                "end_time": end,
                "discount_factor": discount,
                "average_epe": epe,
                "average_ene": ene,
                "counterparty_default_probability": cp_default_probability,
                "own_default_probability": own_default_probability,
                "cva_contribution": cva_contribution,
                "dva_contribution": dva_contribution,
            }
        )

    return {
        "cva": cva,
        "dva": dva,
        "net_credit_xva": -cva + dva,
        "discount_rate": discount_rate,
        "intervals": intervals,
    }
