import ipaddress
import random

from eipdhcp import validate as val
from eipdhcp.netindex import NetIndex
from eipdhcp.rows import build_sites


def random_nets(rng, n):
    nets = []
    for _ in range(n):
        plen = rng.choice([16, 20, 22, 23, 24, 24, 24, 26, 28, 30])
        base = rng.randrange(0, 2 ** 12) << 20          # small address space -> many overlaps
        nets.append(ipaddress.ip_network((base, plen), strict=False))
    return nets


def test_index_matches_brute_force_including_order_and_duplicates():
    rng = random.Random(1234)
    for _ in range(30):
        nets = random_nets(rng, 120)
        index = NetIndex()
        for i, net in enumerate(nets):
            expected = [(o, j) for j, o in enumerate(nets[:i]) if o.overlaps(net)]
            assert index.overlapping(net) == expected
            index.add(net, i)
        # the index keeps tags: look up again after everything is added
        for net in nets[:20]:
            assert index.overlapping(net) == [(o, j) for j, o in enumerate(nets) if o.overlaps(net)]


def reference_offline_overlaps(sites):
    """The previous brute-force logic, kept here as the reference."""
    out, seen = [], {}
    for s in sites:
        key = str(s.network)
        if key in seen:
            continue
        seen[key] = s.line
        for other_key, other_line in list(seen.items()):
            if other_key != key and ipaddress.ip_network(other_key).overlaps(s.network):
                out.append((s.line, f"{s.network} overlaps {other_key} (line {other_line})"))
    return out


def test_validate_offline_overlap_messages_match_the_old_logic():
    rng = random.Random(99)
    rows = [{"space": "S", "site": f"Site {i}", "network": str(n), "dhcp_server": "d", "start_address": "1",
             "end_address": "2", "gateway": "", "dns_servers": "", "failover_channel": ""}
            for i, n in enumerate(random_nets(rng, 150))]
    sites = build_sites(rows)
    got = [(i.line, i.msg) for i in val.validate_offline(sites) if " overlaps " in i.msg and "line" in i.msg]
    assert got == reference_offline_overlaps(sites)
