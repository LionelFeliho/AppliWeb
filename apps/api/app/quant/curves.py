from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class CurveNode:
    tenor: str
    time: float
    zero_rate: float
    quote_id: str


class ZeroCurve:
    """Continuously compounded zero curve with log-discount interpolation."""

    def __init__(self, currency: str, nodes: Iterable[CurveNode]):
        ordered = sorted(nodes, key=lambda node: node.time)
        if not ordered:
            raise ValueError(f"Curve {currency} requires at least one node")
        if any(node.time <= 0 for node in ordered):
            raise ValueError("Curve node times must be positive")
        if len({node.time for node in ordered}) != len(ordered):
            raise ValueError("Curve node times must be unique")
        if any(not math.isfinite(node.zero_rate) for node in ordered):
            raise ValueError("Curve rates must be finite")

        self.currency = currency.upper()
        self.nodes = tuple(ordered)
        self._times = [node.time for node in ordered]
        self._log_discounts = [-node.zero_rate * node.time for node in ordered]

    def discount(self, time: float) -> float:
        if time <= 0:
            return 1.0
        if time <= self._times[0]:
            return math.exp(-self.nodes[0].zero_rate * time)
        if time >= self._times[-1]:
            return math.exp(-self.nodes[-1].zero_rate * time)

        index = bisect.bisect_right(self._times, time)
        left_time = self._times[index - 1]
        right_time = self._times[index]
        weight = (time - left_time) / (right_time - left_time)
        log_discount = (
            self._log_discounts[index - 1] * (1.0 - weight)
            + self._log_discounts[index] * weight
        )
        return math.exp(log_discount)

    def zero_rate(self, time: float) -> float:
        if time <= 0:
            return self.nodes[0].zero_rate
        discount = self.discount(time)
        return -math.log(discount) / time

    def relative_discount(self, start_time: float, end_time: float) -> float:
        if end_time < start_time:
            raise ValueError("end_time must be on or after start_time")
        return self.discount(end_time) / self.discount(start_time)

    def forward_rate(self, start_time: float, end_time: float, accrual: float) -> float:
        if end_time <= start_time:
            raise ValueError("forward end_time must be after start_time")
        if accrual <= 0:
            raise ValueError("forward accrual must be positive")
        return (self.discount(start_time) / self.discount(end_time) - 1.0) / accrual

    def parallel_bump(self, bump_decimal: float) -> "ZeroCurve":
        return ZeroCurve(
            self.currency,
            [
                CurveNode(
                    tenor=node.tenor,
                    time=node.time,
                    zero_rate=node.zero_rate + bump_decimal,
                    quote_id=node.quote_id,
                )
                for node in self.nodes
            ],
        )

    def node_bump(self, node_index: int, bump_decimal: float) -> "ZeroCurve":
        if node_index < 0 or node_index >= len(self.nodes):
            raise IndexError(f"Curve node index out of range: {node_index}")
        return ZeroCurve(
            self.currency,
            [
                CurveNode(
                    tenor=node.tenor,
                    time=node.time,
                    zero_rate=node.zero_rate + (bump_decimal if index == node_index else 0.0),
                    quote_id=node.quote_id,
                )
                for index, node in enumerate(self.nodes)
            ],
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "currency": self.currency,
            "interpolation": "log_linear_discount_factor",
            "nodes": [
                {
                    "tenor": node.tenor,
                    "time": node.time,
                    "zero_rate": node.zero_rate,
                    "discount_factor": self.discount(node.time),
                    "quote_id": node.quote_id,
                }
                for node in self.nodes
            ],
        }
