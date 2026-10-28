"""CSV rows -> Site objects. Everything comes from the CSV."""
import csv
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Optional


def split_list(text):
    """'a,b;c d' -> ['a','b','c','d']"""
    return [x for x in re.split(r"[;,\s]+", text or "") if x]


@dataclass
class Site:
    line: int
    name: str
    space: str = ""
    network: Optional[ipaddress.IPv4Network] = None
    dhcp_server: str = ""
    start: str = ""
    end: str = ""
    gateway: str = ""
    dns: list = field(default_factory=list)
    failover: str = ""
    problems: list = field(default_factory=list)   # errors found while building the site

    @property
    def key(self):
        return str(self.network) if self.network is not None else f"line{self.line}"


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = []
        for raw in reader:
            rows.append({str(k).strip(): (v or "").strip() for k, v in raw.items() if k and isinstance(v, (str, type(None)))})
        return [h.strip() for h in (reader.fieldnames or [])], rows


def build_site(line, row, settings=None):
    site = Site(line=line, name=row.get("site", ""))
    site.space = row.get("space") or (settings.default_space if settings else "")
    site.dhcp_server = row.get("dhcp_server") or (settings.default_dhcp_server if settings else "")
    site.failover = row.get("failover_channel", "")
    site.dns = split_list(row.get("dns_servers"))

    try:
        net = ipaddress.ip_network(row.get("network", ""), strict=True)
        if not isinstance(net, ipaddress.IPv4Network):
            raise ValueError("only IPv4 networks are supported")
        site.network = net
    except ValueError as e:
        site.problems.append(f"network '{row.get('network', '')}': {e}")
        return site

    def resolve(col):
        """Full IP, or a number counted from the network address ('10', '510' in a /23). Empty stays empty."""
        value = row.get(col, "").strip()
        if not value:
            return ""
        try:
            if "." in value:
                return str(ipaddress.ip_address(value))
            return str(site.network.network_address + int(value))
        except ValueError as e:
            site.problems.append(f"{col} '{value}': {e}")
            return ""

    site.start = resolve("start_address")
    site.end = resolve("end_address")
    site.gateway = resolve("gateway")
    return site


def build_sites(rows, settings=None, first_line=2):
    return [build_site(i, r, settings) for i, r in enumerate(rows, start=first_line)]


def filter_sites(sites, names):
    """--site filter (case-insensitive). Returns (selected, unknown_names)."""
    wanted = [n.strip().lower() for n in (names or []) if n.strip()]
    if not wanted:
        return list(sites), []
    selected = [s for s in sites if s.name.strip().lower() in wanted]
    found = {s.name.strip().lower() for s in selected}
    return selected, [n for n in wanted if n not in found]
