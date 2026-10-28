"""All server access. Reads are retried; writes are never retried (no double-creates)."""
import ipaddress
import json
import logging
import time

log = logging.getLogger("dhcp_scopes")

READ_SUFFIXES = ("_list", "_info", "_count")


def q(value):
    """Escape a value for a WHERE clause."""
    return str(value).replace("'", "''")


def ok(code):
    return code in (200, 201)


def net_from_row(row):
    """subnet row -> IPv4Network (start_hostaddr + subnet_size), or None."""
    try:
        if row.get("start_hostaddr"):
            start = ipaddress.ip_address(row["start_hostaddr"])
        else:
            start = ipaddress.ip_address(int(row["start_ip_addr"], 16))
        size = int(row["subnet_size"])
        if size <= 0 or size & (size - 1):
            return None
        return ipaddress.ip_network(f"{start}/{32 - (size.bit_length() - 1)}", strict=False)
    except (KeyError, ValueError, TypeError):
        return None


class Api:
    def __init__(self, sds, retries=3, sleep=time.sleep):
        self.sds = sds
        self.retries = retries
        self._sleep = sleep
        self._servers, self._spaces, self._blocks, self._terminals = {}, {}, {}, {}
        self._pool_names = None

    # ---- low level -------------------------------------------------------
    def _query(self, service, params, allow_raw, option=False):
        attempts = self.retries if (service.endswith(READ_SUFFIXES) or option) else 1
        last = None
        for n in range(1, attempts + 1):
            log.debug("API call  service=%s raw=%s params=%s (try %d)", service, allow_raw, params, n)
            try:
                if option:
                    r = self.sds.query(service, params or {}, option=True, allow_raw=allow_raw, timeout=60)
                else:
                    r = self.sds.query(service, params or {}, allow_raw=allow_raw, timeout=60)
                try:
                    body = json.loads(r.text)
                except ValueError:
                    body = r.text
                log.debug("API reply service=%s status=%s body=%s", service, r.status_code, body)
                return r.status_code, body
            except Exception as e:      # connection / timeout / SDK errors
                last = e
                log.warning("API error service=%s try %d/%d: %s", service, n, attempts, e)
                if n < attempts:
                    self._sleep(n)      # 1s, 2s
        raise last

    def call(self, service, params=None):
        """Service known to the SDK (create / update / list ...)."""
        return self._query(service, params, allow_raw=False)

    def raw(self, service, params=None, option=False):
        """Service the SDK does not map (sent as-is)."""
        return self._query(service, params, allow_raw=True, option=option)

    def sleep(self, seconds):
        self._sleep(seconds)

    # ---- servers / spaces ------------------------------------------------
    def server(self, name):
        if name not in self._servers:
            code, body = self.call("dhcp_server_list", {"WHERE": f"dhcp_name='{q(name)}'"})
            if not ok(code) or not isinstance(body, list) or not body:
                raise RuntimeError(f"DHCP server '{name}' not found ({code})")
            self._servers[name] = body[0]
        return self._servers[name]

    def server_id(self, name):
        return self.server(name)["dhcp_id"]

    def space_id(self, name):
        if name not in self._spaces:
            code, body = self.call("ip_site_list", {"WHERE": f"site_name='{q(name)}'"})
            if not ok(code) or not isinstance(body, list) or not body:
                raise RuntimeError(f"IPAM space '{name}' not found ({code})")
            self._spaces[name] = body[0]["site_id"]
        return self._spaces[name]

    # ---- IPAM ------------------------------------------------------------
    def find_network(self, site_id, net):
        """Existing TERMINAL network with exactly this address/size -> row or None."""
        code, body = self.call("ip_subnet_list", {
            "WHERE": f"site_id='{q(site_id)}' and start_hostaddr='{net.network_address}'"
                     f" and subnet_size={net.num_addresses} and is_terminal='1'"})
        return body[0] if ok(code) and isinstance(body, list) and body else None

    def _load(self, site_id, terminal):
        code, body = self.call("ip_subnet_list", {
            "WHERE": f"site_id='{q(site_id)}' and is_terminal='{1 if terminal else 0}'", "LIMIT": "5000"})
        out = []
        for row in (body if ok(code) and isinstance(body, list) else []):
            n = net_from_row(row)
            if n is not None:
                out.append((n, row.get("subnet_id")))
        return out

    def blocks(self, site_id):
        if site_id not in self._blocks:
            self._blocks[site_id] = self._load(site_id, False)
        return self._blocks[site_id]

    def terminal_networks(self, site_id):
        if site_id not in self._terminals:
            self._terminals[site_id] = self._load(site_id, True)
        return self._terminals[site_id]

    def find_parent_block(self, site_id, net):
        """Smallest block containing net -> (network, id) or None. Loaded once per space."""
        best = None
        for bnet, bid in self.blocks(site_id):
            if net.subnet_of(bnet) and (best is None or bnet.prefixlen > best[0].prefixlen):
                best = (bnet, bid)
        return best

    def add_block(self, site_id, net, block_id):
        self.blocks(site_id).append((net, block_id))

    def get_subnet(self, subnet_id):
        code, body = self.call("ip_subnet_list", {"WHERE": f"subnet_id='{q(subnet_id)}'"})
        return body[0] if ok(code) and isinstance(body, list) and body else None

    def pools_of(self, subnet_id):
        code, body = self.raw("ip_pool_list", {"WHERE": f"subnet_id='{q(subnet_id)}'"})
        return body if ok(code) and isinstance(body, list) else []

    def addresses_of(self, subnet_id):
        """IP addresses registered in a network (empty list when the server cannot list them)."""
        try:
            code, body = self.call("ip_address_list", {"WHERE": f"subnet_id='{q(subnet_id)}'", "LIMIT": "50"})
        except Exception:
            return []
        return body if ok(code) and isinstance(body, list) else []

    def delete_subnet(self, subnet_id):
        """Deletes a network or a block (same service). Never retried."""
        return self.call("ip_subnet_delete", {"subnet_id": subnet_id})

    def forget_subnet(self, site_id, subnet_id):
        """Drop a deleted network/block from the caches."""
        for cache in (self._blocks, self._terminals):
            if site_id in cache:
                cache[site_id] = [(n, i) for n, i in cache[site_id] if str(i) != str(subnet_id)]

    def find_pool(self, subnet_id):
        code, body = self.raw("ip_pool_list", {"WHERE": f"subnet_id='{q(subnet_id)}'"})
        return body[0] if ok(code) and isinstance(body, list) and body else None

    def pool_param_names(self):
        if self._pool_names is None:
            try:
                code, doc = self.raw("ip_pool_add", {}, option=True)
                self._pool_names = {d["name"] for d in doc if isinstance(d, dict) and d.get("param_type") == "in"}
            except Exception:
                self._pool_names = set()
        return self._pool_names

    # ---- DHCP ------------------------------------------------------------
    def find_scope(self, dhcp_id, net_addr):
        code, body = self.call("dhcp_scope_list", {
            "WHERE": f"dhcp_id='{q(dhcp_id)}' and dhcpscope_net_addr='{q(net_addr)}'"})
        return body[0] if ok(code) and isinstance(body, list) and body else None

    def scopes_for(self, net):
        """Every scope (smart + members) of this network."""
        code, body = self.call("dhcp_scope_list", {
            "WHERE": f"dhcpscope_net_addr='{net.network_address}' and dhcpscope_net_mask='{net.netmask}'"})
        return body if ok(code) and isinstance(body, list) else []

    def ranges_of(self, scope_id):
        code, body = self.call("dhcp_range_list", {"WHERE": f"dhcpscope_id='{q(scope_id)}'"})
        return body if ok(code) and isinstance(body, list) else []

    def statics_of(self, scope_id):
        code, body = self.call("dhcp_static_list", {"WHERE": f"dhcpscope_id='{q(scope_id)}'"})
        return body if ok(code) and isinstance(body, list) else []

    def options_of(self, scope_id):
        """{option name: row}"""
        code, body = self.raw("dhcp_scope_options_list", {"dhcpscope_id": scope_id})
        rows = body if ok(code) and isinstance(body, list) else []
        return {r.get("dhcpoption_name"): r for r in rows}
