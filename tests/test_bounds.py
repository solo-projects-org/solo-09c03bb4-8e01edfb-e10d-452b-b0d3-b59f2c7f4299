from __future__ import annotations

import itertools
import random
import sys
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meal_subsidy.bounds import StreetCounts, aggregate_records, compare_streets  # noqa: E402
from meal_subsidy.importer import parse_household_csv  # noqa: E402


def possibilities(eligible, received):
    if eligible == 0:
        return [(0, 0)]
    if received == 1:
        return [(1, 1)]
    if eligible == 1 and received == 0:
        return [(1, 0)]
    if eligible == 1 and received is None:
        return [(1, 0), (1, 1)]
    if eligible is None and received == 0:
        return [(0, 0), (1, 0)]
    if eligible is None and received == 1:
        return [(1, 1)]
    if eligible is None and received is None:
        return [(0, 0), (1, 0), (1, 1)]
    raise AssertionError((eligible, received))


class BoundTests(unittest.TestCase):
    def test_sample_exact_bounds(self):
        content = (ROOT / "sample_data" / "sample_households.csv").read_bytes()
        records, errors = parse_household_csv(content)
        self.assertEqual(errors, [])
        streets = aggregate_records([r.as_mapping() for r in records])
        result = compare_streets(streets, 8, "人民路街道", "建设路街道")
        self.assertTrue(result.positive_pair_exists)
        self.assertFalse(result.zero_denominator_possible)
        self.assertEqual(result.min_value, Fraction(-7, 15))
        self.assertEqual(result.max_value, Fraction(4, 5))
        self.assertEqual(result.min_witness.eligible_1, 5)
        self.assertEqual(result.min_witness.eligible_2, 3)

    def test_no_positive_denominator_pair(self):
        streets = {
            "A": StreetCounts("A", total=1, x=1),
            "B": StreetCounts("B", total=1, x=1),
        }
        result = compare_streets(streets, 0, "A", "B")
        self.assertTrue(result.zero_denominator_possible)
        self.assertEqual(result.zero_denominator_streets, ("A", "B"))
        self.assertFalse(result.positive_pair_exists)
        self.assertIsNone(result.min_value)
        self.assertIsNone(result.max_value)

    def test_zero_not_possible_when_district_total_forces_denominator(self):
        # A has one fully unknown household; B is forced eligible.  With
        # total=2 and an ineligible C, both selected denominators must be 1.
        rows = [
            {"street": "A", "eligible": None, "received": None},
            {"street": "B", "eligible": 1, "received": 0},
            {"street": "C", "eligible": 0, "received": None},
        ]
        streets = aggregate_records(rows)
        result = compare_streets(streets, 2, "A", "B")
        self.assertFalse(result.zero_denominator_possible)
        self.assertTrue(result.positive_pair_exists)

    def test_random_cases_match_exhaustive_completions(self):
        rng = random.Random(20261126)
        names = ["A", "B", "C"]
        for case in range(400):
            raw = [
                ("A", rng.choice([0, 1, None]), rng.choice([0, 1, None])),
                ("B", rng.choice([0, 1, None]), rng.choice([0, 1, None])),
            ]
            if raw[0][1] == 0 and raw[0][2] == 1:
                raw[0] = ("A", 0, 0)
            if raw[1][1] == 0 and raw[1][2] == 1:
                raw[1] = ("B", 0, 0)
            for _ in range(rng.randint(0, 7)):
                eligible = rng.choice([0, 1, None, None])
                received = rng.choice([0, 1, None, None])
                if eligible == 0 and received == 1:
                    received = 0
                raw.append((rng.choice(names), eligible, received))

            streets = aggregate_records(
                {"street": s, "eligible": e, "received": r} for s, e, r in raw
            )
            p_total = sum(s.p for s in streets.values())
            u_total = sum(s.u for s in streets.values())
            total = rng.randint(p_total, u_total)
            result = compare_streets(streets, total, "A", "B")

            values = []
            zero1 = zero2 = False
            options = [
                [(s, ee, rr) for ee, rr in possibilities(e, r)]
                for s, e, r in raw
            ]
            for completion in itertools.product(*options):
                if sum(ee for _, ee, _ in completion) != total:
                    continue
                counts = {s: [0, 0] for s in names}
                for s, ee, rr in completion:
                    counts[s][0] += rr
                    counts[s][1] += ee
                zero1 |= counts["A"][1] == 0
                zero2 |= counts["B"][1] == 0
                if counts["A"][1] > 0 and counts["B"][1] > 0:
                    values.append(
                        Fraction(counts["A"][0], counts["A"][1])
                        - Fraction(counts["B"][0], counts["B"][1])
                    )

            self.assertEqual(result.zero_denominator_possible, zero1 or zero2, case)
            if values:
                self.assertTrue(result.positive_pair_exists, case)
                self.assertEqual(result.min_value, min(values), case)
                self.assertEqual(result.max_value, max(values), case)
            else:
                self.assertFalse(result.positive_pair_exists, case)
                self.assertIsNone(result.min_value, case)


if __name__ == "__main__":
    unittest.main(verbosity=2)
