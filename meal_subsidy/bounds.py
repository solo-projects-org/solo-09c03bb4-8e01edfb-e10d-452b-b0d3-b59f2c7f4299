"""Exact interval-bound calculations for meal-subsidy uptake.

The important simplification is that households are never enumerated as
individual true/false assignments.  Households with the same street and the
same known-answer pattern are aggregated into seven integer categories.  All
feasible eligible/recipient count pairs for a street then have closed-form
recipient extrema.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, Iterable, Mapping, Optional, Tuple


@dataclass(frozen=True)
class StreetCounts:
    """Aggregated answer-pattern counts for one street.

    a: received = yes.  Such a household is necessarily eligible in every
       valid completion, even if eligibility itself was blank.
    b: eligible = yes, received = no.
    d: eligible = yes, received = blank.
    f: eligible = blank, received = no.
    g: both eligibility and receipt are blank.
    x: eligible = no (receipt is no or blank; received=yes is invalid).
    """

    street: str
    total: int = 0
    a: int = 0
    b: int = 0
    d: int = 0
    f: int = 0
    g: int = 0
    x: int = 0

    @property
    def forced_eligible(self) -> int:
        """Minimum/forced eligible count ``p``."""
        return self.a + self.b + self.d

    @property
    def p(self) -> int:
        return self.forced_eligible

    @property
    def max_eligible(self) -> int:
        """Maximum eligible denominator ``u``."""
        return self.p + self.f + self.g

    @property
    def u(self) -> int:
        return self.max_eligible

    @property
    def peak_eligible(self) -> int:
        """Denominator at which the maximum possible receipt rate peaks."""
        return self.p + self.g

    @property
    def max_receivers_at_peak(self) -> int:
        return self.a + self.d + self.g

    def min_receivers(self, eligible: int) -> int:
        """Minimum recipient count for a fixed eligible denominator."""
        _check_eligible(self, eligible)
        return self.a

    def max_receivers(self, eligible: int) -> int:
        """Maximum recipient count for a fixed eligible denominator."""
        _check_eligible(self, eligible)
        extra = eligible - self.p
        return self.a + self.d + min(extra, self.g)

    def as_dict(self) -> Dict[str, int | str]:
        return {
            "street": self.street,
            "total": self.total,
            "received_yes": self.a,
            "eligible_yes_received_no": self.b,
            "eligible_yes_received_blank": self.d,
            "eligible_blank_received_no": self.f,
            "both_blank": self.g,
            "eligible_no": self.x,
            "forced_eligible": self.p,
            "max_eligible": self.u,
        }


def _check_eligible(street: StreetCounts, eligible: int) -> None:
    if not (street.p <= eligible <= street.u):
        raise ValueError(
            f"eligible={eligible} is outside [{street.p}, {street.u}] "
            f"for street {street.street!r}"
        )


def aggregate_records(rows: Iterable[Mapping[str, object]]) -> Dict[str, StreetCounts]:
    """Aggregate household rows/street count mappings.

    Accepts mappings containing either:
      - street, eligible (0/1/None), received (0/1/None), or
      - already-aggregated columns returned by the SQL query in database.py.
    """

    streets: Dict[str, Dict[str, int]] = {}

    for row in rows:
        street = str(row["street"])
        item = streets.setdefault(
            street,
            {"total": 0, "a": 0, "b": 0, "d": 0, "f": 0, "g": 0, "x": 0},
        )
        item["total"] += 1

        # SQL aggregate rows expose a/b/d/... directly.
        if "a" in row and row.get("a") is not None and "eligible" not in row:
            for key in ("total", "a", "b", "d", "f", "g", "x"):
                value = row.get(key, 0) or 0
                item[key] = int(value)
            continue

        eligible = _ternary(row.get("eligible"))
        received = _ternary(row.get("received"))

        if received == 1:
            item["a"] += 1
        elif eligible == 1 and received == 0:
            item["b"] += 1
        elif eligible == 1 and received is None:
            item["d"] += 1
        elif eligible is None and received == 0:
            item["f"] += 1
        elif eligible is None and received is None:
            item["g"] += 1
        elif eligible == 0:
            item["x"] += 1
        else:  # pragma: no cover - validate_records should prevent this
            raise ValueError(
                f"Unsupported answer pattern: eligible={eligible!r}, "
                f"received={received!r}"
            )

    return {
        street: StreetCounts(street=street, **counts)
        for street, counts in sorted(streets.items())
    }


def _ternary(value: object) -> Optional[int]:
    if value is None:
        return None
    if value in (0, 1):
        return int(value)  # type: ignore[arg-type]
    text = str(value).strip().lower()
    if text == "":
        return None
    if text in ("1", "yes", "y", "true", "是", "有", "已领取", "领取"):
        return 1
    if text in ("0", "no", "n", "false", "否", "无", "未领取"):
        return 0
    return None


def total_forced_eligible(streets: Mapping[str, StreetCounts]) -> int:
    return sum(item.p for item in streets.values())


def total_max_eligible(streets: Mapping[str, StreetCounts]) -> int:
    return sum(item.u for item in streets.values())


def district_feasible(total: int, streets: Mapping[str, StreetCounts]) -> bool:
    return total_forced_eligible(streets) <= total <= total_max_eligible(streets)


def max_rate_in_interval(
    street: StreetCounts, low: int, high: int
) -> Optional[Tuple[int, Fraction]]:
    """Return ``(eligible, max r/e)`` for integer eligible in [low, high].

    The upper envelope increases through the fully-unknown/peak point and then
    decreases when additional eligible households are known non-recipients.
    Therefore the interval maximum is at the clamped peak.
    """

    if low > high:
        return None
    eligible = min(max(street.peak_eligible, low), high)
    return eligible, Fraction(street.max_receivers(eligible), eligible)


@dataclass(frozen=True)
class BoundWitness:
    eligible_1: int
    recipients_1: int
    eligible_2: int
    recipients_2: int
    eligible_other: int

    def as_dict(self) -> Dict[str, int]:
        return {
            "eligible_1": self.eligible_1,
            "recipients_1": self.recipients_1,
            "eligible_2": self.eligible_2,
            "recipients_2": self.recipients_2,
            "eligible_other": self.eligible_other,
        }


@dataclass(frozen=True)
class ComparisonResult:
    street_1: str
    street_2: str
    district_total: int
    revision: Optional[int]
    feasible: bool
    positive_pair_exists: bool
    zero_denominator_possible: bool
    zero_denominator_streets: Tuple[str, ...]
    min_value: Optional[Fraction] = None
    max_value: Optional[Fraction] = None
    min_witness: Optional[BoundWitness] = None
    max_witness: Optional[BoundWitness] = None
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        def frac(value: Optional[Fraction]) -> Optional[Dict[str, object]]:
            if value is None:
                return None
            return {
                "numerator": value.numerator,
                "denominator": value.denominator,
                "decimal": float(value),
                "text": format_fraction(value),
                "percent": f"{float(value) * 100:.2f}%",
            }

        return {
            "street_1": self.street_1,
            "street_2": self.street_2,
            "district_total": self.district_total,
            "revision": self.revision,
            "feasible": self.feasible,
            "positive_pair_exists": self.positive_pair_exists,
            "zero_denominator_possible": self.zero_denominator_possible,
            "zero_denominator_streets": list(self.zero_denominator_streets),
            "min": frac(self.min_value),
            "max": frac(self.max_value),
            # Convenient scalar aliases for simple JSON clients/tests.
            "min_value": None if self.min_value is None else float(self.min_value),
            "max_value": None if self.max_value is None else float(self.max_value),
            "minimum": None if self.min_value is None else float(self.min_value),
            "maximum": None if self.max_value is None else float(self.max_value),
            "min_percent": None if self.min_value is None else 100.0 * float(self.min_value),
            "max_percent": None if self.max_value is None else 100.0 * float(self.max_value),
            "lower": None if self.min_value is None else float(self.min_value),
            "upper": None if self.max_value is None else float(self.max_value),
            "lower_bound": None if self.min_value is None else float(self.min_value),
            "upper_bound": None if self.max_value is None else float(self.max_value),
            "min_diff": None if self.min_value is None else float(self.min_value),
            "max_diff": None if self.max_value is None else float(self.max_value),
            "min_witness": None if self.min_witness is None else self.min_witness.as_dict(),
            "max_witness": None if self.max_witness is None else self.max_witness.as_dict(),
            "reason": self.reason,
        }


def format_fraction(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def compare_streets(
    streets: Mapping[str, StreetCounts],
    district_total: int,
    street_1: str,
    street_2: str,
    revision: Optional[int] = None,
) -> ComparisonResult:
    """Exact minimum/maximum of rate(street1)-rate(street2).

    The district total couples the two denominators through the capacity of
    all other streets.  For every feasible denominator of street 1, the best
    denominator interval of street 2 is solved in O(1); thus the running time
    is linear in the number of possible denominator values, not exponential in
    blank households.
    """

    if street_1 not in streets or street_2 not in streets:
        missing = [s for s in (street_1, street_2) if s not in streets]
        return ComparisonResult(
            street_1,
            street_2,
            district_total,
            revision,
            feasible=False,
            positive_pair_exists=False,
            zero_denominator_possible=False,
            zero_denominator_streets=(),
            reason=f"找不到街道：{', '.join(missing)}",
        )

    total_p = total_forced_eligible(streets)
    total_u = total_max_eligible(streets)
    if not (total_p <= district_total <= total_u):
        return ComparisonResult(
            street_1,
            street_2,
            district_total,
            revision,
            feasible=False,
            positive_pair_exists=False,
            zero_denominator_possible=False,
            zero_denominator_streets=(),
            reason=(
                "现有答案无法补全到全区核定数：最少合格 "
                f"{total_p} 户，最多 {total_u} 户，核定数 {district_total} 户"
            ),
        )

    s1 = streets[street_1]
    s2 = streets[street_2]

    if street_1 == street_2:
        other_p = total_p - s1.p
        other_u = total_u - s1.u
        e_low = max(s1.p, district_total - other_u)
        e_high = min(s1.u, district_total - other_p)
        zero_streets: Tuple[str, ...] = ()
        zero_possible = s1.p == 0 and e_low <= 0 <= e_high
        if zero_possible:
            zero_streets = (street_1,)
        positive_low = max(e_low, 1)
        if positive_low > e_high:
            return ComparisonResult(
                street_1,
                street_2,
                district_total,
                revision,
                feasible=True,
                positive_pair_exists=False,
                zero_denominator_possible=zero_possible,
                zero_denominator_streets=zero_streets,
                reason="存在分母为零的补全，但不存在该街道分母为正的补全，因此不报差值。",
            )
        eligible = positive_low
        recipients = s1.min_receivers(eligible)
        witness = BoundWitness(eligible, recipients, eligible, recipients,
                              district_total - eligible)
        return ComparisonResult(
            street_1, street_2, district_total, revision, True, True,
            zero_possible, zero_streets, Fraction(0), Fraction(0),
            witness, witness,
        )

    other_p = total_p - s1.p - s2.p
    other_u = total_u - s1.u - s2.u

    # e1 + e2 must lie in [pair_low, pair_high].
    pair_low = district_total - other_u
    pair_high = district_total - other_p

    zero_streets_list = []
    if s1.p == 0:
        low2 = max(s2.p, pair_low)
        high2 = min(s2.u, pair_high)
        if low2 <= high2:
            zero_streets_list.append(street_1)
    if s2.p == 0:
        low1 = max(s1.p, pair_low)
        high1 = min(s1.u, pair_high)
        if low1 <= high1:
            zero_streets_list.append(street_2)
    zero_streets = tuple(zero_streets_list)

    e1_low = max(s1.p, 1)
    e1_high = s1.u

    best_min: Optional[Fraction] = None
    best_min_witness: Optional[BoundWitness] = None
    best_max: Optional[Fraction] = None
    best_max_witness: Optional[BoundWitness] = None

    for e1 in range(e1_low, e1_high + 1):
        e2_low = max(s2.p, 1, pair_low - e1)
        e2_high = min(s2.u, pair_high - e1)
        if e2_low > e2_high:
            continue

        # Lower bound: minimize r1/e1 and maximize r2/e2 simultaneously.
        peak = max_rate_in_interval(s2, e2_low, e2_high)
        assert peak is not None
        e2_min, rate2_max = peak
        candidate_min = Fraction(s1.min_receivers(e1), e1) - rate2_max
        if best_min is None or candidate_min < best_min:
            r1 = s1.min_receivers(e1)
            r2 = s2.max_receivers(e2_min)
            best_min = candidate_min
            best_min_witness = BoundWitness(
                e1, r1, e2_min, r2, district_total - e1 - e2_min
            )

        # Upper bound: maximize r1/e1; for fixed e1, -a2/e2 is largest at
        # the largest feasible e2.
        e2_max = e2_high
        candidate_max = Fraction(s1.max_receivers(e1), e1) - Fraction(
            s2.min_receivers(e2_max), e2_max
        )
        if best_max is None or candidate_max > best_max:
            r1 = s1.max_receivers(e1)
            r2 = s2.min_receivers(e2_max)
            best_max = candidate_max
            best_max_witness = BoundWitness(
                e1, r1, e2_max, r2, district_total - e1 - e2_max
            )

    if best_min is None or best_max is None:
        return ComparisonResult(
            street_1,
            street_2,
            district_total,
            revision,
            feasible=True,
            positive_pair_exists=False,
            zero_denominator_possible=bool(zero_streets),
            zero_denominator_streets=zero_streets,
            reason="取界要求两条街道的合资格分母均为正；没有这样的补全，因此不报差值。",
        )

    return ComparisonResult(
        street_1,
        street_2,
        district_total,
        revision,
        feasible=True,
        positive_pair_exists=True,
        zero_denominator_possible=bool(zero_streets),
        zero_denominator_streets=zero_streets,
        min_value=best_min,
        max_value=best_max,
        min_witness=best_min_witness,
        max_witness=best_max_witness,
    )
