# -*- coding: utf-8 -*-
import unittest

# Assumes RangeSet is defined in rangeset.py in the same directory
from zstacklib.utils.rangeset import RangeSet

class TestRangeSet(unittest.TestCase):
    def test_add_basic_and_merge(self):
        rs = RangeSet()
        rs.add(2, 5)
        rs.add(7, 10)
        self.assertEqual(list(rs), [(2, 5), (7, 10)])

        # Touching intervals should merge for half-open [l, r)
        rs.add(5, 7)
        self.assertEqual(list(rs), [(2, 10)])

        # Overlapping with existing should still be one interval
        rs.add(1, 3)
        self.assertEqual(list(rs), [(1, 10)])

        # Chain merges; note [14,15) is a gap, so two intervals remain
        rs.add(15, 18)
        rs.add(12, 14)
        rs.add(10, 12)
        self.assertEqual(list(rs), [(1, 14), (15, 18)])

    def test_add_empty_and_reverse(self):
        rs = RangeSet()
        rs.add(5, 5)  # empty interval, no-op
        self.assertEqual(list(rs), [])

        # Reversed endpoints should be normalized
        rs.add(8, 3)
        self.assertEqual(list(rs), [(3, 8)])

    def test_remove_split_and_chop(self):
        rs = RangeSet()
        rs.add(2, 10)  # [2,10)

        # Removing middle should split into two
        rs.remove(3, 8)
        self.assertEqual(list(rs), [(2, 3), (8, 10)])

        # Removing left-side range eliminates [2,3)
        rs.remove(0, 2)
        self.assertEqual(list(rs), [(2, 3), (8, 10)])

        # Removing right tail chops to [8,9)
        rs.remove(9, 20)
        self.assertEqual(list(rs), [(2, 3), (8, 9)])

        # Removing exact match clears it
        rs.remove(8, 9)
        self.assertEqual(list(rs), [(2, 3)])

    def test_remove_edges_and_outside(self):
        rs = RangeSet()
        rs.add(10, 20)

        # Removing an empty range does nothing
        rs.remove(5, 5)
        self.assertEqual(list(rs), [(10, 20)])

        # Removing non-overlapping left range does nothing
        rs.remove(0, 3)
        self.assertEqual(list(rs), [(10, 20)])

        # Removing non-overlapping right range does nothing
        rs.remove(21, 30)
        self.assertEqual(list(rs), [(10, 20)])

        # Removing touching left boundary [5,10) should leave [10,20) unchanged
        rs.remove(5, 10)
        self.assertEqual(list(rs), [(10, 20)])

        # Removing touching right boundary [20,25) should leave unchanged
        rs.remove(20, 25)
        self.assertEqual(list(rs), [(10, 20)])

        # Removing superset clears it
        rs.remove(0, 100)
        self.assertEqual(list(rs), [])

    def test_contains(self):
        rs = RangeSet()
        rs.add(5, 8)  # [5,8)
        self.assertTrue(5 in rs)    # left boundary included
        self.assertTrue(7 in rs)    # interior
        self.assertFalse(8 in rs)   # right boundary excluded (half-open)
        self.assertFalse(4 in rs)   # outside

    def test_len_total_coverage(self):
        rs = RangeSet()
        rs.add(1, 4)   # covers {1,2,3} -> length 3
        rs.add(6, 10)  # covers {6,7,8,9} -> length 4
        self.assertEqual(len(rs), 7)

        # Bridging [4,6) merges to [1,10) -> length 9
        rs.add(4, 6)
        self.assertEqual(len(rs), 9)

    def test_missing_basic_and_limit(self):
        rs = RangeSet()
        rs.add(2, 5)
        rs.add(7, 10)
        # Universe [0,12)
        self.assertEqual(rs.missing(12, 10), [(0, 2), (5, 7), (10, 12)])
        self.assertEqual(rs.missing(12, 2), [(0, 2), (5, 7)])

    def test_missing_edge_cases(self):
        # Empty set
        rs = RangeSet()
        self.assertEqual(rs.missing(5, 10), [(0, 5)])
        # total = 0 -> no universe
        self.assertEqual(rs.missing(0, 10), [])

        # Negative and out-of-universe intervals should be clamped/ignored
        rs2 = RangeSet()
        rs2.add(-5, -2)
        rs2.add(3, 4)
        rs2.add(10, 20)
        self.assertEqual(rs2.missing(8, 10), [(0, 3), (4, 8)])

        # Full cover
        rs3 = RangeSet()
        rs3.add(0, 5)
        self.assertEqual(rs3.missing(5, 10), [])
        rs3.add(5, 10)
        self.assertEqual(rs3.missing(10, 10), [])

    def test_bulk_merge_and_stability(self):
        rs = RangeSet()
        # Insert in mixed order; should merge into [0,25)
        for seg in [(10, 15), (0, 2), (5, 7), (2, 5), (7, 10), (20, 25), (15, 20)]:
            rs.add(*seg)
        self.assertEqual(list(rs), [(0, 25)])

        # Adding empty interval is a no-op
        rs.add(25, 25)
        self.assertEqual(list(rs), [(0, 25)])

    def test_idempotent_add(self):
        rs = RangeSet()
        rs.add(1, 4)
        rs.add(1, 4)
        rs.add(2, 3)
        self.assertEqual(list(rs), [(1, 4)])

    def test_large_gaps_missing_limit(self):
        rs = RangeSet()
        rs.add(100, 200)
        rs.add(400, 500)
        # Universe [0, 1000), limit 2 should return first two gaps only
        self.assertEqual(rs.missing(1000, 2), [(0, 100), (200, 400)])

    def test_fully_covered(self):
        rs = RangeSet()
        rs.add(2, 5)
        rs.add(5, 8)  # merged to [2,8)
        self.assertTrue(rs.covered(2, 8))
        self.assertTrue(rs.covered(3, 7))
        self.assertFalse(rs.covered(1, 3))  # gap at [1,2)
        self.assertFalse(rs.covered(7, 10)) # gap at [8,10)
        # Empty interval is trivially covered
        self.assertTrue(rs.covered(5, 5))

if __name__ == "__main__":
    unittest.main(verbosity=2)