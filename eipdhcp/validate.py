"""Check the CSV before anything is sent to the server."""
import ipaddress
import logging
from dataclasses import dataclass

log = logging.getLogger("dhcp_scopes")

ERROR, WARN, INFO = "ERROR", "WARN", "INFO"


@dataclass
class Issue:
    level: str
    line: int
    site: str
    msg: str


def _ip(text):
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def validate_offline(sites):
    issues = []

    def add(level, s, msg):
        issues.append(Issue(level, s.line, s.name, msg))

    seen_net, seen_name = {}, {}
    for s in sites:
        for p in s.problems:
            add(ERROR, s, p)
        if not s.name:
            add(ERROR, s, "column 'site' is empty")
        if s.network is None:
            continue
        net = s.network
        if net.prefixlen > 30:
            add(ERROR, s, f"{net} is too small for a DHCP scope (use /30 or larger)")
        for col, val in (("space", s.space), ("dhcp_server", s.dhcp_server)):
            if not val:
                add(ERROR, s, f"'{col}' is empty (set it in the CSV, or as a default in .env)")

        start, end, gw = _ip(s.start), _ip(s.end), _ip(s.gateway)
        if not s.start or not s.end:
            if not any("start_address" in p or "end_address" in p for p in s.problems):
                add(ERROR, s, "start_address / end_address are missing in the CSV")
        else:
            if start is None or end is None:
                add(ERROR, s, "start/end are not valid IP addresses")
            else:
                for label, ip in (("start", start), ("end", end)):
                    if ip not in net:
                        add(ERROR, s, f"{label} {ip} is outside {net}")
                    elif ip in (net.network_address, net.broadcast_address):
                        add(ERROR, s, f"{label} {ip} is the network/broadcast address of {net}")
                if start > end:
                    add(ERROR, s, f"start {start} is after end {end}")
        if not s.gateway:
            add(WARN, s, "no gateway: the router option will not be set")
        elif gw is None:
            add(ERROR, s, f"gateway '{s.gateway}' is not a valid IP address")
        else:
            if gw not in net:
                add(ERROR, s, f"gateway {gw} is outside {net}")
            elif start and end and start <= gw <= end:
                add(WARN, s, f"gateway {gw} lies inside the DHCP range {start}-{end}")
        if not s.dns:
            add(WARN, s, "no DNS servers: the DNS option will not be set")
        for d in s.dns:
            if _ip(d) is None:
                add(ERROR, s, f"DNS server '{d}' is not a valid IP address")
        if not s.failover:
            add(WARN, s, "no failover_channel: the range will not be assigned to a member server")

        key = str(net)
        if key in seen_net:
            add(ERROR, s, f"network {net} is already listed on line {seen_net[key]}")
        else:
            seen_net[key] = s.line
            for other_key, other_line in list(seen_net.items()):
                if other_key != key and ipaddress.ip_network(other_key).overlaps(net):
                    add(ERROR, s, f"{net} overlaps {other_key} (line {other_line})")
        lname = s.name.strip().lower()
        if lname:
            if lname in seen_name:
                add(WARN, s, f"site name also used on line {seen_name[lname]}")
            else:
                seen_name[lname] = s.line
    return issues


def validate_online(sites, api, skip_lines=(), action="create"):
    """Checks that need the server. Never raises: a failed check becomes a WARN."""
    issues = []

    def add(level, s, msg):
        issues.append(Issue(level, s.line, s.name, msg))

    for s in sites:
        if s.network is None or s.line in skip_lines:
            continue
        try:
            sid = api.space_id(s.space) if s.space else None
        except Exception as e:
            add(ERROR, s, str(e))
            sid = None
        try:
            dhcp_id = api.server_id(s.dhcp_server) if s.dhcp_server else None
        except Exception as e:
            add(ERROR, s, str(e))
            dhcp_id = None
        if s.failover:
            try:
                api.server_id(s.failover)
            except Exception:
                add(ERROR, s, f"failover server '{s.failover}' not found in DHCP -> Servers")
        try:
            if sid is not None:
                for n, nid in api.terminal_networks(sid):
                    if n == s.network:
                        add(INFO, s, f"network already in IPAM (id={nid})" + ("; create reuses it" if action == "create" else ""))
                    elif n.overlaps(s.network):
                        add(ERROR, s, f"{s.network} overlaps existing IPAM network {n} (id={nid})")
            if dhcp_id is not None:
                scope = api.find_scope(dhcp_id, str(s.network.network_address))
                if scope:
                    add(INFO, s, f"DHCP scope exists (id={scope['dhcpscope_id']})" + ("; create will skip this site, use update to change it" if action == "create" else ""))
        except Exception as e:
            add(WARN, s, f"could not check existing objects: {e}")
    return issues


def validate(sites, api=None, action="create"):
    issues = validate_offline(sites)
    if api is not None:
        bad = {i.line for i in issues if i.level == ERROR}
        issues += validate_online(sites, api, skip_lines=bad, action=action)
    order = {ERROR: 0, WARN: 1, INFO: 2}
    return sorted(issues, key=lambda i: (i.line, order[i.level]))


def counts(issues):
    return {lvl: sum(1 for i in issues if i.level == lvl) for lvl in (ERROR, WARN, INFO)}


def error_lines(issues):
    return {i.line for i in issues if i.level == ERROR}


def validate_delete(sites, api=None):
    """Delete only needs space + network (+ site name for the safety check)."""
    issues, seen = [], {}
    for s in sites:
        net_problems = [p for p in s.problems if p.startswith("network")]
        for p in net_problems:
            issues.append(Issue(ERROR, s.line, s.name, p))
        if s.network is None:
            continue
        if not s.space:
            issues.append(Issue(ERROR, s.line, s.name, "'space' is empty"))
        if s.key in seen:
            issues.append(Issue(ERROR, s.line, s.name, f"network {s.network} is already listed on line {seen[s.key]}"))
        seen.setdefault(s.key, s.line)
        if api is not None and s.space:
            try:
                api.space_id(s.space)
            except Exception as e:
                issues.append(Issue(ERROR, s.line, s.name, str(e)))
    return sorted(issues, key=lambda i: i.line)
