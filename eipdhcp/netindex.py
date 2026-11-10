"""Fast overlap lookups for IPv4 networks.

Two CIDR networks overlap only when one contains the other, so a network's overlaps are its
supernets (at most 33 exact lookups) plus the networks that start inside its address range
(one bisect). That is O(log n + hits) per query instead of comparing against every entry.
"""
import bisect


class NetIndex:
    def __init__(self):
        self._items = []        # position -> (network, tag)
        self._exact = {}        # (network int, prefixlen) -> [positions]
        self._starts = []       # sorted (network int, prefixlen, position)

    def add(self, net, tag=None):
        pos = len(self._items)
        self._items.append((net, tag))
        start = int(net.network_address)
        self._exact.setdefault((start, net.prefixlen), []).append(pos)
        bisect.insort(self._starts, (start, net.prefixlen, pos))
        return pos

    def overlapping(self, net):
        """Every added network that overlaps `net` (equal ones included), in the order they were
        added, as a list of (network, tag)."""
        start, end = int(net.network_address), int(net.broadcast_address)
        hits = set()
        for plen in range(net.prefixlen + 1):                    # equal or larger (supernets)
            shift = 32 - plen
            hits.update(self._exact.get(((start >> shift) << shift, plen), ()))
        lo = bisect.bisect_left(self._starts, (start, -1, -1))
        hi = bisect.bisect_right(self._starts, (end, 33, float("inf")))
        for _, plen, pos in self._starts[lo:hi]:                 # smaller (inside net)
            if plen > net.prefixlen:
                hits.add(pos)
        return [self._items[pos] for pos in sorted(hits)]
