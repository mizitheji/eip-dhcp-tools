"""A small stateful stand-in for the SOLIDserver REST API, shaped after what the real server returned."""
import ipaddress
import json
from urllib.parse import parse_qsl, urlencode


class Resp:
    def __init__(self, code, body):
        self.status_code = code
        self.text = "" if body is None else json.dumps(body)


def parse_where(where):
    conds = []
    for part in (where or "").split(" and "):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        v = v.strip()
        if v.startswith("'"):
            v = v[1:-1].replace("''", "'")
        conds.append((k.strip(), v))
    return conds


def match(row, where):
    return all(str(row.get(k, "")) == v for k, v in parse_where(where))


class FakeSDS:
    SMART = "2"

    def __init__(self):
        self.calls = []
        self._n = 1000
        self.fail = {}                       # service -> (status, body): force an answer
        self.reject_pool_dhcprange = False
        self.spaces = [{"site_id": "2", "site_name": "TestSpace"}]
        self.servers = [
            {"dhcp_id": "2", "dhcp_name": "smart.dhcp.example", "dhcp_type": "vdhcp"},
            {"dhcp_id": "12", "dhcp_name": "fo1.dhcp", "dhcp_type": "ipm"},
            {"dhcp_id": "6", "dhcp_name": "member2.dhcp", "dhcp_type": "ipm"},
        ]
        self.subnets, self.pools, self.scopes, self.ranges, self.options = [], [], [], [], []
        self.statics, self.addresses = [], []
        self.auto_scope = False              # real server: a network with a failover channel gets its scope made for it
        self.auto_router = False             # ... and maybe the router option from the gateway
        self.cascade = True                  # deleting the IPAM network also removes the DHCP scope/range (like the GUI)

    # ---- helpers ---------------------------------------------------------
    def _id(self):
        self._n += 1
        return str(self._n)

    def names(self, service):
        return [c[1] for c in self.calls if c[0] == service]

    def count(self, *services):
        return sum(1 for c in self.calls if c[0] in services)

    def writes(self):
        return [c for c in self.calls if c[0].endswith(("_create", "_update", "_add"))]

    def member_servers(self):
        return [s for s in self.servers if s["dhcp_id"] != self.SMART]

    # ---- entry point (same signature as SOLIDserverRest.query) ----------------
    def query(self, service, params=None, option=False, timeout=None, allow_raw=False):
        params = dict(params or {})
        self.calls.append((service, params, option))
        if service in self.fail:
            code, body = self.fail[service]
            return Resp(code, body)
        h = getattr(self, "h_" + service, None)
        if h is None:
            return Resp(400, [{"errno": "3", "errmsg": "unknown service", "name": service}])
        return h(params, option)

    def _list(self, rows, params):
        out = [r for r in rows if match(r, params.get("WHERE"))]
        return Resp(200, out) if out else Resp(204, None)

    # ---- lookups ------------------------------------------------------------------
    def h_ip_site_list(self, p, o):   return self._list(self.spaces, p)
    def h_dhcp_server_list(self, p, o): return self._list(self.servers, p)
    def h_ip_subnet_list(self, p, o): return self._list(self.subnets, p)
    def h_ip_pool_list(self, p, o):   return self._list(self.pools, p)
    def h_dhcp_scope_list(self, p, o): return self._list(self.scopes, p)
    def h_dhcp_range_list(self, p, o): return self._list(self.ranges, p)
    def h_dhcp_static_list(self, p, o): return self._list(self.statics, p)
    def h_ip_address_list(self, p, o): return self._list(self.addresses, p)

    def h_dhcp_scope_options_list(self, p, o):
        return Resp(200, [r for r in self.options if r["dhcpscope_id"] == str(p.get("dhcpscope_id"))])

    # ---- IPAM ---------------------------------------------------------------------
    def h_ip_subnet_create(self, p, o):
        net = ipaddress.ip_network(f"{p['subnet_addr']}/{p['subnet_prefix']}")
        terminal = str(p.get("is_terminal")) == "1"
        if terminal:
            blocks = [s for s in self.subnets if s["is_terminal"] == "0" and s["site_id"] == str(p["site_id"])]
            inside = [b for b in blocks if net.subnet_of(self._net(b))]
            if not inside:
                return Resp(400, {"errno": "5001", "errmsg": "Subnet is not in a block"})
            if str(p.get("parent_subnet_id")) not in {b["subnet_id"] for b in inside}:
                return Resp(400, {"errno": "5002", "errmsg": "bad parent_subnet_id"})
        row = {"subnet_id": self._id(), "site_id": str(p["site_id"]), "start_hostaddr": p["subnet_addr"],
               "subnet_size": str(net.num_addresses), "is_terminal": "1" if terminal else "0", "subnet_ip_used_size": "0",
               "subnet_name": p["subnet_name"],
               "subnet_class_parameters": urlencode({"dns_update": "0", **dict(parse_qsl(p.get("subnet_class_parameters", "")))})}
        self.subnets.append(row)
        cp = dict(parse_qsl(p.get("subnet_class_parameters", "")))
        if terminal and cp.get("gateway"):                     # IPAM registers the gateway as an address
            self.addresses.append({"hostaddr": cp["gateway"], "subnet_id": row["subnet_id"], "name": "gateway"})
            row["subnet_ip_used_size"] = "1"
        if terminal and self.auto_scope and cp.get("dhcp_failover_name"):
            self._auto_scope(row, net, cp)
        return Resp(201, [{"ret_oid": row["subnet_id"]}])

    def _auto_scope(self, row, net, cp):
        base = {"dhcpscope_name": row["subnet_name"], "dhcpscope_net_addr": row["start_hostaddr"],
                "dhcpscope_net_mask": str(net.netmask), "dhcpfailover_name": cp["dhcp_failover_name"]}
        made = [dict(base, dhcpscope_id=self._id(), dhcp_id=self.SMART, dhcp_name="smart.dhcp.example")]
        for m in self.member_servers():
            made.append(dict(base, dhcpscope_id=self._id(), dhcp_id=m["dhcp_id"], dhcp_name=m["dhcp_name"]))
        self.scopes.extend(made)
        if self.auto_router and cp.get("gateway"):
            for sc in made:
                self.options.append({"dhcpoption_name": "option routers", "dhcpoption_value": cp["gateway"] + ";",
                                     "dhcpscopeoption_id": self._id(), "dhcpscope_id": sc["dhcpscope_id"]})

    @staticmethod
    def _net(row):
        size = int(row["subnet_size"])
        return ipaddress.ip_network(f"{row['start_hostaddr']}/{32 - (size.bit_length() - 1)}")

    def h_ip_subnet_update(self, p, o):
        row = next((s for s in self.subnets if s["subnet_id"] == str(p["subnet_id"])), None)
        if row is None:
            return Resp(400, {"errno": "1", "errmsg": "no such subnet"})
        if "subnet_class_parameters" in p:
            merged = dict(parse_qsl(row["subnet_class_parameters"]))
            merged.update(dict(parse_qsl(p["subnet_class_parameters"])))
            row["subnet_class_parameters"] = urlencode(merged)
        if "subnet_name" in p:
            row["subnet_name"] = p["subnet_name"]
        return Resp(201, [{"ret_oid": row["subnet_id"]}])

    def h_ip_pool_add(self, p, o):
        if o:
            names = ["subnet_id", "pool_name", "start_addr", "end_addr", "pool_read_only", "pool_class_parameters", "pool_id"]
            return Resp(200, [{"errno": "0", "param_type": "in", "name": n} for n in names])
        if self.reject_pool_dhcprange and "dhcprange" in p.get("pool_class_parameters", ""):
            return Resp(400, {"errno": "9", "errmsg": "range exists"})
        row = {"pool_id": self._id(), "subnet_id": str(p["subnet_id"]), "pool_name": p["pool_name"],
               "start_hostaddr": p["start_addr"], "end_hostaddr": p["end_addr"],
               "pool_read_only": p.get("pool_read_only", "0"),
               "pool_class_parameters": "dns_update=0&" + p.get("pool_class_parameters", "")}
        self.pools.append(row)
        return Resp(201, [{"ret_oid": row["pool_id"]}])

    def h_ip_pool_update(self, p, o):
        row = next((x for x in self.pools if x["pool_id"] == str(p["pool_id"])), None)
        if row is None:
            return Resp(400, {"errno": "1", "errmsg": "no such pool"})
        row["start_hostaddr"], row["end_hostaddr"] = p["start_addr"], p["end_addr"]
        return Resp(201, [{"ret_oid": row["pool_id"]}])

    # ---- DHCP scopes / ranges ---------------------------------------------------------
    def _scope(self, sid):
        return next((s for s in self.scopes if s["dhcpscope_id"] == str(sid)), None)

    def _members_of(self, scope):
        return [s for s in self.scopes if s["dhcp_id"] != self.SMART
                and s["dhcpscope_net_addr"] == scope["dhcpscope_net_addr"]
                and s["dhcpscope_net_mask"] == scope["dhcpscope_net_mask"]]

    def h_dhcp_scope_create(self, p, o):
        if str(p["dhcp_id"]) != self.SMART:
            return Resp(400, {"errno": "7", "errmsg": "DHCP server is in read_only mode (SMART DHCP member)"})
        base = {"dhcpscope_name": p["dhcpscope_name"], "dhcpscope_net_addr": p["dhcpscope_net_addr"],
                "dhcpscope_net_mask": p["dhcpscope_net_mask"], "dhcpfailover_name": p.get("dhcpfailover_name", "")}
        row = dict(base, dhcpscope_id=self._id(), dhcp_id=self.SMART, dhcp_name="smart.dhcp.example")
        self.scopes.append(row)
        for m in self.member_servers():                     # the smart server pushes copies to members
            self.scopes.append(dict(base, dhcpscope_id=self._id(), dhcp_id=m["dhcp_id"], dhcp_name=m["dhcp_name"]))
        return Resp(201, [{"ret_oid": row["dhcpscope_id"]}])

    def h_dhcp_scope_update(self, p, o):
        sc = self._scope(p["dhcpscope_id"])
        if sc is None:
            return Resp(400, {"errno": "1", "errmsg": "no such scope"})
        for s in [sc] + self._members_of(sc):
            if "dhcpscope_name" in p:
                s["dhcpscope_name"] = p["dhcpscope_name"]
            if "dhcpfailover_name" in p:
                s["dhcpfailover_name"] = p["dhcpfailover_name"]
        return Resp(201, [{"ret_oid": sc["dhcpscope_id"]}])

    def h_dhcp_range_create(self, p, o):
        sc = self._scope(p["dhcpscope_id"])
        if sc is None:
            return Resp(400, {"errno": "1", "errmsg": "no such scope"})
        if sc["dhcp_id"] != self.SMART:
            return Resp(400, {"errno": "7", "errmsg": "DHCP server is in read_only mode (SMART DHCP member)"})
        base = {"dhcprange_start_addr": p["dhcprange_start_addr"], "dhcprange_end_addr": p["dhcprange_end_addr"],
                "dhcprange_failover_name": p.get("dhcprange_failover_name", ""), "dhcprange_lease_count": "0"}
        row = dict(base, dhcprange_id=self._id(), dhcpscope_id=sc["dhcpscope_id"], dhcp_name=sc["dhcp_name"])
        self.ranges.append(row)
        for m in self._members_of(sc):
            self.ranges.append(dict(base, dhcprange_id=self._id(), dhcpscope_id=m["dhcpscope_id"], dhcp_name=m["dhcp_name"]))
        return Resp(201, [{"ret_oid": row["dhcprange_id"]}])

    def h_dhcp_range_update(self, p, o):
        row = next((r for r in self.ranges if r["dhcprange_id"] == str(p["dhcprange_id"])), None)
        if row is None:
            return Resp(400, {"errno": "1", "errmsg": "no such range"})
        sc = self._scope(row["dhcpscope_id"])
        old = (row["dhcprange_start_addr"], row["dhcprange_end_addr"])
        targets = [row] + [r for r in self.ranges if r is not row and (r["dhcprange_start_addr"], r["dhcprange_end_addr"]) == old
                           and self._scope(r["dhcpscope_id"]) in self._members_of(sc)]
        for r in targets:
            r["dhcprange_start_addr"], r["dhcprange_end_addr"] = p["dhcprange_start_addr"], p["dhcprange_end_addr"]
            if "dhcprange_failover_name" in p:
                r["dhcprange_failover_name"] = p["dhcprange_failover_name"]
        return Resp(201, [{"ret_oid": row["dhcprange_id"]}])

    # ---- options ------------------------------------------------------------------------
    def h_dhcp_option_add(self, p, o):
        sc = self._scope(p["dhcpscope_id"])
        if sc is None:
            return Resp(400, {"errno": "1", "errmsg": "no such scope"})
        if not p["dhcpoption_name"].startswith("option "):
            return Resp(400, {"errno": "2628", "errmsg": "Can't find dhcpoptiondef name", "optiondef_name": p["dhcpoption_name"]})
        if sc["dhcp_id"] != self.SMART:
            return Resp(400, {"errno": "7", "errmsg": "DHCP server is in read_only mode (SMART DHCP member)"})
        if any(r["dhcpscope_id"] == sc["dhcpscope_id"] and r["dhcpoption_name"] == p["dhcpoption_name"] for r in self.options):
            return Resp(400, {"errno": "8", "errmsg": "option already exists"})
        base = {"dhcpoption_name": p["dhcpoption_name"], "dhcpoption_value": p["dhcpoption_value"]}
        row = dict(base, dhcpscopeoption_id=self._id(), dhcpscope_id=sc["dhcpscope_id"])
        self.options.append(row)
        for m in self._members_of(sc):
            self.options.append(dict(base, dhcpscopeoption_id=self._id(), dhcpscope_id=m["dhcpscope_id"]))
        return Resp(201, [{"ret_oid": row["dhcpscopeoption_id"]}])

    def h_dhcp_option_update(self, p, o):
        sc = self._scope(p["dhcpscope_id"])
        rows = [r for r in self.options if r["dhcpscope_id"] == str(p["dhcpscope_id"]) and r["dhcpoption_name"] == p["dhcpoption_name"]]
        if sc is None or not rows:
            return Resp(400, {"errno": "1", "errmsg": "no such option"})
        for s in [sc] + self._members_of(sc):
            for r in self.options:
                if r["dhcpscope_id"] == s["dhcpscope_id"] and r["dhcpoption_name"] == p["dhcpoption_name"]:
                    r["dhcpoption_value"] = p["dhcpoption_value"]
        return Resp(201, [{"ret_oid": rows[0]["dhcpscopeoption_id"]}])

    # ---- delete -----------------------------------------------------------------------
    def h_ip_subnet_delete(self, p, o):
        row = next((x for x in self.subnets if x["subnet_id"] == str(p["subnet_id"])), None)
        if row is None:
            return Resp(400, {"errno": "1", "errmsg": "no such subnet"})
        net = self._net(row)
        if row["is_terminal"] == "0":
            inside = [x for x in self.subnets if x is not row and x["site_id"] == row["site_id"] and self._net(x).subnet_of(net)]
            if inside:
                return Resp(400, {"errno": "5003", "errmsg": "block is not empty"})
        self.subnets.remove(row)
        self.addresses = [a for a in self.addresses if a["subnet_id"] != row["subnet_id"]]
        if row["is_terminal"] == "1":
            self.pools = [x for x in self.pools if x["subnet_id"] != row["subnet_id"]]
            if self.cascade:
                gone = {x["dhcpscope_id"] for x in self.scopes
                        if x["dhcpscope_net_addr"] == row["start_hostaddr"] and x["dhcpscope_net_mask"] == str(net.netmask)}
                self.scopes = [x for x in self.scopes if x["dhcpscope_id"] not in gone]
                self.ranges = [x for x in self.ranges if x["dhcpscope_id"] not in gone]
                self.options = [x for x in self.options if x["dhcpscope_id"] not in gone]
        return Resp(200, [{"ret_oid": row["subnet_id"]}])
