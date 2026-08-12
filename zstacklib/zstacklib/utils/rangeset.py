# -*- coding: utf-8 -*-
from bisect import bisect_left, bisect_right

class RangeSet(object):
    """
    Non-overlapping merged half-open intervals [l, r) with integer endpoints.
    Stores as a sorted list of (l, r) by l. Supports add, remove, membership,
    total covered length, iteration, and computing missing gaps within [0, total).
    """
    __slots__ = ('iv',)

    def __init__(self, iterable=None):
        # Sorted by start: [(l1, r1), (l2, r2), ...], with r_i <= l_{i+1}
        self.iv = []
        if iterable:
            for l, r in iterable:
                self.add(l, r)

    def add(self, l, r):
        # Insert half-open [l, r); normalize if needed
        if r < l:
            l, r = r, l
        if l == r:
            return  # empty interval, no effect

        iv = self.iv
        # Find first interval whose start >= l
        i = bisect_left(iv, (l, -10**30))
        # If previous interval overlaps or touches (in half-open, touch is r_prev >= l)
        if i and iv[i-1][1] >= l:
            i -= 1

        # Merge forward while overlapping or touching: next.start <= r
        j = i
        n = len(iv)
        L, R = l, r
        while j < n and iv[j][0] <= R:
            L = min(L, iv[j][0])
            R = max(R, iv[j][1])
            j += 1

        iv[i:j] = [(L, R)]

    def remove(self, l, r):
        # Remove half-open [l, r)
        if r < l:
            l, r = r, l
        if l == r:
            return

        iv = self.iv
        i = bisect_left(iv, (l, -10**30))
        if i and iv[i-1][1] > l:  # strictly overlaps in half-open sense
            i -= 1

        out = []
        k = 0

        # Copy intervals strictly before potential overlaps
        while k < i:
            out.append(iv[k])
            k += 1

        # Process overlaps with [l, r)
        while k < len(iv) and iv[k][0] < r:
            a, b = iv[k]
            # Left fragment: [a, min(b, l))
            if a < l:
                left_end = min(b, l)
                if a < left_end:
                    out.append((a, left_end))
            # Right fragment: [max(a, r), b)
            if b > r:
                right_start = max(a, r)
                if right_start < b:
                    out.append((right_start, b))
            k += 1

        # Copy the rest
        while k < len(iv):
            out.append(iv[k])
            k += 1

        self.iv = out

    def contains(self, x):
        # True if x in any [l, r)
        iv = self.iv
        i = bisect_right(iv, (x, 10**30)) - 1
        return i >= 0 and iv[i][0] <= x < iv[i][1]

    def __contains__(self, x):
        return self.contains(x)

    def __len__(self):
        # Total covered length (number of integers) for integer endpoints
        return sum(r - l for l, r in self.iv)

    def __iter__(self):
        for l, r in self.iv:
            yield (l, r)

    def __repr__(self):
        return ", ".join("[%s, %s)" % (l, r) for l, r in self.iv)

    def missing(self, total, limit):
        """
        Return up to `limit` missing half-open gaps within [0, total),
        as a list of (l, r) tuples, where each represents [l, r).
        - total: integer >= 0; universe is [0, total)
        - limit: max number of gaps to return; if <= 0, returns []
        Notes:
        - Intervals outside the universe are ignored/clamped.
        - If the set is empty, returns [(0, total)] (or truncated by limit).
        """
        res = []
        if limit <= 0 or total <= 0:
            return res

        cur = 0
        iv = self.iv

        for (l, r) in iv:
            if cur >= total:
                break
            # Skip intervals entirely before 0
            if r <= 0:
                continue
            # Clamp l to at least 0
            if l < 0:
                l = 0
            # If current cursor is before this interval's start, that's a gap
            if cur < l:
                start = cur
                end = min(l, total)
                if start < end:
                    res.append((start, end))
                    if len(res) >= limit:
                        return res
            # Advance cursor to the end of this covered interval
            if r > cur:
                cur = r

        # Tail gap after the last interval
        if cur < total and len(res) < limit:
            res.append((cur, total))

        if len(res) > limit:
            del res[limit:]

        return res

    def covered(self, l, r):
        """
        Return True iff the entire [l, r) is covered by the current intervals.
        """
        if r < l:
            l, r = r, l
        if l == r:
            return True  # empty interval is trivially covered

        cur = l
        for a, b in self.iv:
            if b <= cur:
                continue
            if a > cur:
                # gap before the next covering interval
                return False
            # a <= cur < b, advance cursor
            cur = max(cur, b)
            if cur >= r:
                return True
        return cur >= r
