"""Plan what a site needs, execute the plan, or verify the site against the server.

mode "create": new sites only. A site whose DHCP scope already exists is skipped untouched.
mode "update": reconcile an existing site with the CSV (create what is missing, fix what differs).
verify       : the update comparison without applying anything.
"""
import json
import ipaddress
import logging
import os
import re
from datetime import datetime
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode

from . import ui
from .api import ok

log = logging.getLogger("dhcp_scopes")

ALL_PARTS = frozenset({"ipam", "dhcp", "pool", "options"})


# ── data types ──────────────────────────────────────────────────────────────
@dataclass
class Outcome:
    ok: bool
    text: str = ""
    oid: Optional[str] = None
    kind: Optional[str] = None            # block | network | scope | range | pool | option


@dataclass
class Step:
    area: str
    action: str                           # create | update | ok | skip | warn
    text: str
    run: Optional[Callable] = None
    critical: bool = True                 # a failed critical step stops the remaining steps


@dataclass
class Plan:
    steps: list = field(default_factory=list)
    skip_reason: str = ""
    skip_area: str = "SCOPE"
    skip_status: str = "skipped"          # "refused" = a safety check said no (counts as a problem)


@dataclass
class Ctx:
    api: object
    settings: object
    site: object
    parts: frozenset = ALL_PARTS
    mode: str = "create"
    keep_names: bool = False
    any_name: bool = False        # delete: do not require the IPAM name to equal the CSV name
    allow_used: bool = False      # delete: do not refuse when IPAM shows IPs in use
    ids: dict = field(default_factory=dict)


@dataclass
class SiteOutcome:
    status: str                           # applied | planned | in-sync | skipped | partial | failed | verified | mismatch | invalid
    detail: str = ""
    changes: int = 0                      # planned or applied create/update steps
    failures: int = 0


# ── helpers ─────────────────────────────────────────────────────────────────
def _first(body):
    if isinstance(body, list) and body:
        body = body[0]
    return body if isinstance(body, dict) else {}


def err_text(body):
    d = _first(body)
    return d.get("errmsg") or (json.dumps(d) if d else str(body)[:300])


def err_no(body):
    return str(_first(body).get("errno", ""))


def ret_oid(body):
    return _first(body).get("ret_oid")


def outcome(code, body, kind, what):
    if ok(code):
        oid = ret_oid(body)
        return Outcome(True, f"{what} (id={oid})", oid, kind)
    return Outcome(False, f"{what} failed: {err_text(body)} [{code}]")


def parse_cp(text):
    return dict(parse_qsl(text or "", keep_blank_values=True))


def build_cp(d):
    return urlencode(d)


def want_cp(site):
    cp = {}
    if site.gateway:
        cp["gateway"] = site.gateway
    if site.failover:
        cp["dhcp_failover_name"] = site.failover
    return cp


def norm_opt(value):
    return "".join(str(value or "").split()).rstrip(";")


def _need(ctx, key, what):
    if not ctx.ids.get(key):
        raise RuntimeError(f"{what} id is unknown (an earlier step did not run)")
    return ctx.ids[key]


# ── IPAM ────────────────────────────────────────────────────────────────────
def _create_block(c):
    s = c.site
    code, body = c.api.call("ip_subnet_create", {
        "site_id": c.ids["site_id"], "subnet_addr": str(s.network.network_address),
        "subnet_prefix": s.network.prefixlen, "subnet_name": s.name,
        "is_terminal": "0", "subnet_level": "0"})
    out = outcome(code, body, "block", "block created")
    if out.ok:
        c.ids["parent_block"] = out.oid
        c.api.add_block(c.ids["site_id"], s.network, out.oid)
    return out


def _create_network(c):
    s = c.site
    parent = _need(c, "parent_block", "parent block")
    params = {"site_id": c.ids["site_id"], "subnet_addr": str(s.network.network_address),
              "subnet_prefix": s.network.prefixlen, "subnet_name": s.name,
              "is_terminal": "1", "parent_subnet_id": parent}
    cp = want_cp(s)
    if cp:
        params["subnet_class_parameters"] = build_cp(cp)
    code, body = c.api.call("ip_subnet_create", params)
    out = outcome(code, body, "network", "network created")
    if out.ok:
        c.ids["network"] = out.oid
    return out


def _update_network(new_cp, new_name):
    def run(c):
        params = {"subnet_id": _need(c, "network", "network")}
        if new_cp is not None:
            params["subnet_class_parameters"] = build_cp(new_cp)
        if new_name:
            params["subnet_name"] = new_name
        code, body = c.api.call("ip_subnet_update", params)
        return outcome(code, body, None, "network updated")
    return run


def plan_ipam(ctx, steps):
    api, s = ctx.api, ctx.site
    sid = api.space_id(s.space)
    ctx.ids["site_id"] = sid
    row = api.find_network(sid, s.network)
    want = want_cp(s)
    parent = api.find_parent_block(sid, s.network)
    if parent:
        ctx.ids["parent_block"] = parent[1]

    if row is None:
        if parent is None:
            steps.append(Step("IPAM", "create", f"block {s.network} '{s.name}' (no existing block contains it)", _create_block))
        extra = "  ".join(f"{k}={v}" for k, v in want.items())
        steps.append(Step("IPAM", "create", f"network {s.network} '{s.name}'  {extra}".rstrip(), _create_network))
        return

    ctx.ids["network"] = row["subnet_id"]
    if ctx.mode == "create":
        steps.append(Step("IPAM", "ok", f"network exists (id={row['subnet_id']})"))
        return
    cur = parse_cp(row.get("subnet_class_parameters"))
    changes, merged = [], dict(cur)
    for k, v in want.items():
        if cur.get(k) != v:
            changes.append(f"{k}: {cur.get(k) or '-'} → {v}")
            merged[k] = v
    new_name = None
    if not ctx.keep_names and row.get("subnet_name") and row.get("subnet_name") != s.name:
        changes.append(f"name: {row.get('subnet_name')} → {s.name}")
        new_name = s.name
    if changes:
        steps.append(Step("IPAM", "update", "network " + "; ".join(changes),
                          _update_network(merged if merged != cur else None, new_name)))
    else:
        steps.append(Step("IPAM", "ok", f"network in sync (id={row['subnet_id']})"))
    if parent is None:
        steps.append(Step("IPAM", "warn", "no existing block contains this network"))


# ── DHCP scope / range ──────────────────────────────────────────────────────
def _create_scope(c):
    s = c.site
    # creating the IPAM network with a failover channel / gateway can make the server create the
    # DHCP scope by itself, so look again right before creating it
    existing = c.api.find_scope(c.ids["dhcp_id"], str(s.network.network_address))
    if existing:
        c.ids["scope"] = existing["dhcpscope_id"]
        return Outcome(True, f"scope already there, made by the server (id={existing['dhcpscope_id']})",
                       existing["dhcpscope_id"], "scope")
    params = {"dhcp_id": c.ids["dhcp_id"], "dhcpscope_name": s.name,
              "dhcpscope_net_addr": str(s.network.network_address), "dhcpscope_net_mask": str(s.network.netmask)}
    if s.failover:
        params["dhcpfailover_name"] = s.failover
    code, body = c.api.call("dhcp_scope_create", params)
    out = outcome(code, body, "scope", "scope created")
    if out.ok:
        sid = out.oid
        if not sid:
            row = c.api.find_scope(c.ids["dhcp_id"], str(s.network.network_address))
            sid = row["dhcpscope_id"] if row else None
        if not sid:
            return Outcome(False, "scope created but its id could not be found")
        c.ids["scope"] = sid
    return out


def _update_scope(new_name, new_failover):
    def run(c):
        params = {"dhcpscope_id": _need(c, "scope", "scope")}
        if new_name:
            params["dhcpscope_name"] = new_name
        if new_failover:
            params["dhcpfailover_name"] = new_failover
        code, body = c.api.call("dhcp_scope_update", params)
        return outcome(code, body, None, "scope updated")
    return run


def plan_scope(ctx, steps, scope):
    s = ctx.site
    if scope is None:
        fo = f"  failover={s.failover}" if s.failover else ""
        steps.append(Step("SCOPE", "create", f"'{s.name}' mask={s.network.netmask} on {s.dhcp_server}{fo}", _create_scope))
        return
    ctx.ids["scope"] = scope["dhcpscope_id"]
    if ctx.mode == "create":
        steps.append(Step("SCOPE", "ok", f"scope exists (id={scope['dhcpscope_id']})"))
        return
    changes, new_name, new_fo = [], None, None
    if not ctx.keep_names and scope.get("dhcpscope_name") and scope.get("dhcpscope_name") != s.name:
        changes.append(f"name: {scope.get('dhcpscope_name')} → {s.name}")
        new_name = s.name
    if s.failover and scope.get("dhcpfailover_name") != s.failover:
        changes.append(f"failover: {scope.get('dhcpfailover_name') or '-'} → {s.failover}")
        new_fo = s.failover
    if changes:
        steps.append(Step("SCOPE", "update", "scope " + "; ".join(changes), _update_scope(new_name, new_fo)))
    else:
        steps.append(Step("SCOPE", "ok", f"scope in sync (id={scope['dhcpscope_id']})"))


def _create_range(c):
    s = c.site
    for r in c.api.ranges_of(_need(c, "scope", "scope")):
        if r.get("dhcprange_start_addr") == s.start and r.get("dhcprange_end_addr") == s.end:
            return Outcome(True, f"range already there (id={r.get('dhcprange_id')})", r.get("dhcprange_id"), "range")
    params = {"dhcpscope_id": _need(c, "scope", "scope"),
              "dhcprange_start_addr": s.start, "dhcprange_end_addr": s.end}
    if s.failover:
        params["dhcprange_failover_name"] = s.failover
    code, body = c.api.call("dhcp_range_create", params)
    return outcome(code, body, "range", "range created")


def _update_range(range_id):
    def run(c):
        s = c.site
        params = {"dhcprange_id": range_id, "dhcprange_start_addr": s.start, "dhcprange_end_addr": s.end}
        if s.failover:
            params["dhcprange_failover_name"] = s.failover
        code, body = c.api.call("dhcp_range_update", params)
        return outcome(code, body, None, "range updated")
    return run


def plan_range(ctx, steps, scope):
    s = ctx.site
    ranges = ctx.api.ranges_of(scope["dhcpscope_id"]) if scope else []
    fo = f"  failover={s.failover}" if s.failover else ""
    text = f"{s.start} – {s.end}{fo}"
    if not ranges:
        steps.append(Step("RANGE", "create", text, _create_range))
        return
    exact = [r for r in ranges if r.get("dhcprange_start_addr") == s.start and r.get("dhcprange_end_addr") == s.end]
    cand = exact[0] if exact else (ranges[0] if len(ranges) == 1 else None)
    if cand is None:
        steps.append(Step("RANGE", "warn", f"{len(ranges)} ranges exist and none is {s.start} – {s.end}; not touching them"))
        return
    changes = []
    if not exact:
        changes.append(f"{cand.get('dhcprange_start_addr')} – {cand.get('dhcprange_end_addr')} → {s.start} – {s.end}")
    if s.failover and cand.get("dhcprange_failover_name") != s.failover:
        changes.append(f"failover: {cand.get('dhcprange_failover_name') or '-'} → {s.failover}")
    if not changes:
        steps.append(Step("RANGE", "ok", f"range in sync {s.start} – {s.end} (id={cand.get('dhcprange_id')})"))
    elif ctx.mode == "update":
        steps.append(Step("RANGE", "update", "range " + "; ".join(changes), _update_range(cand.get("dhcprange_id"))))
    else:
        steps.append(Step("RANGE", "skip", "range differs (" + "; ".join(changes) + "); use update"))


# ── IPAM pool ───────────────────────────────────────────────────────────────
def _create_pool(c):
    s = c.site
    existing = c.api.find_pool(_need(c, "network", "network"))
    if existing:
        return Outcome(True, f"pool already there (id={existing.get('pool_id')})", existing.get("pool_id"), "pool")
    names = c.api.pool_param_names()

    def pick(*cand):
        return next((x for x in cand if x in names), cand[0])
    base = {pick("subnet_id"): _need(c, "network", "network"), pick("pool_name"): "DHCP",
            pick("start_addr", "start_hostaddr"): s.start, pick("end_addr", "end_hostaddr"): s.end,
            pick("pool_read_only"): "1"}
    variants = [("dhcprange=1", dict(base, **{pick("pool_class_parameters"): "dhcprange=1"})),
                ("plain", base)]
    last = ""
    for label, params in variants:
        code, body = c.api.raw("ip_pool_add", params)
        if ok(code):
            return Outcome(True, f"pool created, variant {label} (id={ret_oid(body)})", ret_oid(body), "pool")
        last = f"{err_text(body)} [{code}]"
        log.info("pool variant %s rejected: %s", label, last)
    return Outcome(False, f"pool failed: {last}")


def _update_pool(pool_id):
    def run(c):
        s = c.site
        row = c.api.find_pool(_need(c, "network", "network"))      # replication may have fixed it already
        if row and row.get("start_hostaddr") == s.start and row.get("end_hostaddr") == s.end:
            return Outcome(True, "pool already matches (followed the range)")
        code, body = c.api.call("ip_pool_update", {"pool_id": pool_id, "start_addr": s.start, "end_addr": s.end})
        return outcome(code, body, None, "pool updated")
    return run


def plan_pool(ctx, steps):
    s = ctx.site
    net_id = ctx.ids.get("network")
    pool = ctx.api.find_pool(net_id) if net_id else None
    text = f"DHCP {s.start} – {s.end}"
    if pool is None:
        steps.append(Step("POOL", "create", text, _create_pool, critical=False))
        return
    range_differs = pool.get("start_hostaddr") != s.start or pool.get("end_hostaddr") != s.end
    diffs = []
    if range_differs:
        diffs.append(f"{pool.get('start_hostaddr')} – {pool.get('end_hostaddr')} → {s.start} – {s.end}")
    if "dhcprange=1" not in (pool.get("pool_class_parameters") or ""):
        diffs.append("not linked to the DHCP range (dhcprange=1 missing)")
    if not diffs:
        steps.append(Step("POOL", "ok", f"pool in sync (id={pool.get('pool_id')})"))
    elif ctx.mode == "update" and range_differs:
        steps.append(Step("POOL", "update", "pool " + "; ".join(diffs), _update_pool(pool.get("pool_id")), critical=False))
    else:
        # create mode never edits; an unlinked pool cannot be fixed through the API here
        steps.append(Step("POOL", "warn" if ctx.mode == "update" else "skip", "pool " + "; ".join(diffs)))


# ── DHCP options ────────────────────────────────────────────────────────────
def _opt_add(label, names, value):
    def run(c):
        suffix = c.settings.opt_value_suffix
        have = c.api.options_of(_need(c, "scope", "scope"))
        found = next((have[n] for n in names if n in have), None)
        if found:                                           # e.g. the router option made from the gateway
            note = "" if norm_opt(found.get("dhcpoption_value")) == norm_opt(value) else \
                f" with another value ({norm_opt(found.get('dhcpoption_value'))}); use update to change it"
            return Outcome(True, f"{label} already set{note}", found.get("dhcpscopeoption_id"), "option")
        last = ""
        for name in names:
            code, body = c.api.raw("dhcp_option_add", {
                "dhcpoption_type": c.settings.opt_type, "dhcpscope_id": _need(c, "scope", "scope"),
                "dhcpoption_name": name,
                "dhcpoption_value": value if value.endswith(suffix) else value + suffix})
            if ok(code):
                return Outcome(True, f"{label} added as '{name}' ({value})", ret_oid(body), "option")
            last = err_text(body)
            if err_no(body) != "2628":          # 2628 = unknown option name -> try the next candidate
                break
        return Outcome(False, f"{label} not added: {last}")
    return run


def _opt_update(label, name, value):
    def run(c):
        suffix = c.settings.opt_value_suffix
        code, body = c.api.call("dhcp_option_update", {
            "dhcpoption_type": c.settings.opt_type, "dhcpscope_id": _need(c, "scope", "scope"),
            "dhcpoption_name": name,
            "dhcpoption_value": value if value.endswith(suffix) else value + suffix})
        return outcome(code, body, None, f"{label} updated")
    return run


def wanted_options(ctx):
    s, st = ctx.site, ctx.settings
    out = []
    if s.gateway:
        out.append(("router", st.opt_router_names, s.gateway))
    if s.dns:
        out.append(("dns", st.opt_dns_names, ",".join(s.dns)))
    return out


def plan_options(ctx, steps, scope):
    existing = ctx.api.options_of(scope["dhcpscope_id"]) if scope else {}
    for label, names, value in wanted_options(ctx):
        cur = next((existing[n] for n in names if n in existing), None)
        if cur is None:
            steps.append(Step("OPT", "create", f"{label} = {value}", _opt_add(label, names, value), critical=False))
        elif norm_opt(cur.get("dhcpoption_value")) == norm_opt(value):
            steps.append(Step("OPT", "ok", f"{label} in sync ({value})"))
        elif ctx.mode == "update":
            steps.append(Step("OPT", "update", f"{label}: {norm_opt(cur.get('dhcpoption_value'))} → {value}",
                              _opt_update(label, cur.get("dhcpoption_name"), value), critical=False))
        else:
            steps.append(Step("OPT", "skip", f"{label} exists with another value ({norm_opt(cur.get('dhcpoption_value'))}); use update"))


# ── planning a site ─────────────────────────────────────────────────────────
def plan_site(ctx):
    s, api, parts = ctx.site, ctx.api, ctx.parts
    if ctx.mode == "delete":
        return plan_delete(ctx)
    if s.problems:
        raise ValueError("; ".join(s.problems))
    plan = Plan()
    ctx.ids["dhcp_id"] = api.server_id(s.dhcp_server)
    scope = api.find_scope(ctx.ids["dhcp_id"], str(s.network.network_address))

    if ctx.mode == "create" and "dhcp" in parts and scope is not None:
        plan.skip_reason = f"scope already exists (id={scope['dhcpscope_id']}); use update to change it"
        return plan
    if "dhcp" not in parts and "options" in parts and scope is None:
        raise RuntimeError(f"no scope for {s.network} on {s.dhcp_server}; create the site first")

    if "ipam" in parts:
        plan_ipam(ctx, plan.steps)
    if "dhcp" in parts:
        plan_scope(ctx, plan.steps, scope)
        plan_range(ctx, plan.steps, scope)
    elif scope is not None:
        ctx.ids["scope"] = scope["dhcpscope_id"]
    if "pool" in parts and "ipam" in parts:
        plan_pool(ctx, plan.steps)
    if "options" in parts:
        plan_options(ctx, plan.steps, scope)
    return plan



# ── delete: IPAM network (+ its block); the DHCP scope / range / pool follow, like in the GUI ──
def _snapshot(c):
    """Save a copy of every object that is about to disappear (to recreate by hand if needed)."""
    s, api = c.site, c.api
    data = {"taken": datetime.now().isoformat(timespec="seconds"), "site": s.name, "network": s.key,
            "network_row": c.ids.get("network_row"), "block_row": c.ids.get("block_row"),
            "pools": api.pools_of(c.ids["network"]), "scopes": []}
    for sc in api.scopes_for(s.network):
        sid = sc["dhcpscope_id"]
        data["scopes"].append({"scope": sc, "ranges": api.ranges_of(sid), "options": list(api.options_of(sid).values())})
    folder = os.path.join(c.settings.runs_dir, "snapshots")
    os.makedirs(folder, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", s.key)
    path = os.path.join(folder, f"{datetime.now():%Y%m%d_%H%M%S}_{slug}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    c.ids["snapshot"] = path
    return Outcome(True, f"copy of all objects saved: {path}")


def _delete_subnet(c, key, what):
    sid = _need(c, key, what)
    code, body = c.api.delete_subnet(sid)
    if ok(code):
        c.api.forget_subnet(c.ids["site_id"], sid)
        return Outcome(True, f"{what} deleted (id={sid})")
    return Outcome(False, f"{what} not deleted: {err_text(body)} [{code}]")


def _delete_network(c):
    return _delete_subnet(c, "network", "network")


def _delete_block(c):
    return _delete_subnet(c, "block", "block")


def _leftovers(c):
    s, api = c.site, c.api
    left = []
    if api.find_network(c.ids["site_id"], s.network):
        left.append("IPAM network")
    scopes = api.scopes_for(s.network)
    if scopes:
        left.append(f"{len(scopes)} DHCP scope(s) ({', '.join(x.get('dhcp_name', '?') for x in scopes)})")
        if any(api.ranges_of(x["dhcpscope_id"]) for x in scopes):
            left.append("DHCP range(s)")
    if api.pools_of(c.ids["network"]):
        left.append("IPAM pool")
    return left


def _check_gone(c):
    """Wait a little for replication, then confirm nothing is left behind."""
    left = []
    for attempt in range(3):
        left = _leftovers(c)
        if not left:
            return Outcome(True, "IPAM network, DHCP scope(s), range(s) and pool are gone")
        if attempt < 2:
            c.api.sleep(2)
    return Outcome(False, "still present: " + "; ".join(left) +
                   " - the DHCP side may still be syncing; check the GUI or run --verify later")


def _auto_addresses(network):
    """Network and broadcast address of a subnet (registered by the server itself), as strings."""
    try:
        net = ipaddress.ip_network(str(network), strict=False)
    except ValueError:
        return set()
    return {str(net.network_address), str(net.broadcast_address)}


def plan_delete(ctx):
    s, api, plan = ctx.site, ctx.api, Plan()
    if s.network is None:
        raise ValueError("; ".join(s.problems) or "invalid network")
    if not s.space:
        raise ValueError("'space' is empty")
    sid = api.space_id(s.space)
    ctx.ids["site_id"] = sid
    row = api.find_network(sid, s.network)
    scopes = api.scopes_for(s.network)

    plan.skip_area = "SAFE"
    if row is None:
        if scopes:
            plan.skip_status = "refused"
            plan.skip_reason = ("no IPAM network found, but DHCP scope(s) exist for it; this tool deletes through IPAM, "
                                "remove the scope in the GUI")
        else:
            plan.skip_reason = "not found in IPAM or DHCP: nothing to delete"
        return plan
    plan.skip_status = "refused"
    ctx.ids["network"], ctx.ids["network_row"] = row["subnet_id"], row

    # safety checks: the first failure refuses the whole site
    name = (row.get("subnet_name") or "").strip()
    if not ctx.any_name and name.lower() != s.name.strip().lower():
        plan.skip_reason = f"the IPAM network is named '{name}' but the CSV says '{s.name}'; refusing (--any-name overrides)"
        return plan
    used_raw = row.get("subnet_ip_used_size")
    used = None
    try:
        used = int(used_raw) if used_raw not in (None, "") else None
    except ValueError:
        pass
    gw = parse_cp(row.get("subnet_class_parameters")).get("gateway") or s.gateway
    usage_note = "IPAM usage unknown" if used is None else f"IPAM usage {used}"
    if used is not None and used > 0 and not ctx.allow_used:
        addrs = api.addresses_of(row["subnet_id"])
        if addrs:
            # the server registers the network and broadcast address itself; they are not "in use"
            auto = _auto_addresses(s.network)
            seen = [a.get("hostaddr") for a in addrs if a.get("hostaddr")]
            others = [h for h in seen if h != gw and h not in auto]
            if others:
                more = " ..." if len(others) > 5 else ""
                plan.skip_reason = (f"{len(others)} IP address(es) in use in IPAM besides the gateway: "
                                    f"{', '.join(others[:5])}{more}; refusing (--allow-used overrides)")
                return plan
            usage_note = ("only the gateway and the network/broadcast addresses are registered in IPAM"
                          if any(h in auto for h in seen) else "only the gateway address is registered in IPAM")
        elif used <= (1 if gw else 0):
            usage_note = "1 registered address, taken to be the gateway"
        else:
            plan.skip_reason = f"{used} IP address(es) are in use in IPAM; refusing (--allow-used overrides)"
            return plan
    lease_by, statics = {}, 0
    for sc in scopes:
        for rg in api.ranges_of(sc["dhcpscope_id"]):
            try:
                n = int(rg.get("dhcprange_lease_count") or 0)
            except ValueError:
                n = 0
            if n:
                lease_by[sc.get("dhcp_name", "?")] = lease_by.get(sc.get("dhcp_name", "?"), 0) + n
        statics += len(api.statics_of(sc["dhcpscope_id"]))
    if lease_by:
        detail = ", ".join(f"{n} on {name}" for name, n in lease_by.items())
        plan.skip_reason = f"active DHCP leases: {detail}; refusing (wait until they expire)"
        return plan
    if statics:
        plan.skip_reason = f"{statics} static DHCP lease(s) exist; refusing (remove them first)"
        return plan
    plan.skip_status, plan.skip_area = "skipped", "SCOPE"
    usage = usage_note
    plan.steps.append(Step("CHECK", "ok", f"{len(scopes)} DHCP scope(s), no active or static leases, {usage}, name matches"))
    if used is None:
        plan.steps.append(Step("CHECK", "warn", "the server did not report IP usage; the IPAM usage check was skipped"))

    # the block: only when it is exactly this network and holds nothing else
    parent = api.find_parent_block(sid, s.network)
    block_id = None
    if parent is None:
        plan.steps.append(Step("IPAM", "ok", "no parent block (only the network is deleted)"))
    elif parent[0] != s.network:
        plan.steps.append(Step("IPAM", "ok", f"parent block {parent[0]} is larger than the network: kept"))
    else:
        brow = api.get_subnet(parent[1]) or {}
        bname = (brow.get("subnet_name") or "").strip()
        inner = [str(n) for n, i in api.blocks(sid) + api.terminal_networks(sid)
                 if str(i) not in (str(parent[1]), str(row["subnet_id"])) and n.subnet_of(s.network)]
        if inner:
            plan.steps.append(Step("IPAM", "ok", f"block {parent[0]} kept: it also contains {', '.join(inner)}"))
        elif not ctx.any_name and bname.lower() != s.name.strip().lower():
            plan.steps.append(Step("IPAM", "ok", f"block {parent[0]} kept: it is named '{bname}', not '{s.name}'"))
        else:
            block_id = parent[1]
            ctx.ids["block"], ctx.ids["block_row"] = block_id, brow

    plan.steps.append(Step("SNAP", "snapshot", f"save a copy of every object first ({ctx.settings.runs_dir}/snapshots)", _snapshot))
    plan.steps.append(Step("IPAM", "delete", f"network {s.network} '{name}' (id={row['subnet_id']}); the DHCP scope, range and pool follow",
                           _delete_network))
    if block_id:
        plan.steps.append(Step("IPAM", "delete", f"block {s.network} (id={block_id})", _delete_block))
    plan.steps.append(Step("CHECK", "check", "confirm the DHCP scope(s), range(s) and pool are gone", _check_gone, critical=False))
    return plan


# ── executing a plan ────────────────────────────────────────────────────────
def execute(ctx, plan, dry, rec=None, ledger=None):
    """Print + (when not dry) apply the plan. Returns SiteOutcome."""
    if plan.skip_reason:
        ui.step(plan.skip_area, "fail" if plan.skip_status == "refused" else "skip", plan.skip_reason)
        if rec is not None:
            rec["status"] = plan.skip_status
            rec["steps"].append({"area": plan.skip_area, "status": plan.skip_status, "text": plan.skip_reason})
        return SiteOutcome(plan.skip_status, plan.skip_reason)

    changes = failures = 0
    stopped = False
    for st in plan.steps:
        if st.action in ("ok", "skip", "warn"):
            ui.step(st.area, {"ok": "ok", "skip": "skip", "warn": "warn"}[st.action], st.text)
            continue
        if st.action in ("create", "update", "delete"):
            changes += 1
        if stopped:
            ui.step(st.area, "skip", f"not attempted (an earlier step failed): {st.text}")
            continue
        if dry:
            ui.step(st.area, "dry", f"({st.action}) {st.text}")
            continue
        try:
            out = st.run(ctx)
        except Exception as e:                       # SDK error, network error, missing id ...
            log.exception("step %s failed", st.area)
            hint = " (SDK too old? pip install --upgrade SOLIDserverRest)" if "SDSServiceError" in type(e).__name__ else ""
            out = Outcome(False, f"{st.text}: {e}{hint}")
        if out.ok:
            ui.step(st.area, "ok", out.text)
            if rec is not None:
                if out.kind:
                    _record_created(rec, out.kind, out.oid)
                elif st.action == "delete":
                    rec.setdefault("deleted", []).append(f"{st.area}: {out.text}")
                elif st.action == "update":
                    rec["updated"].append(f"{st.area}: {st.text}")
                rec["steps"].append({"area": st.area, "status": "ok", "text": out.text})
        else:
            failures += 1
            ui.step(st.area, "fail", out.text)
            if rec is not None:
                rec["steps"].append({"area": st.area, "status": "fail", "text": out.text})
            if st.critical:
                stopped = True
        if ledger is not None:
            ledger.save()

    if dry:
        return SiteOutcome("planned" if changes else "in-sync", f"{changes} change(s) planned" if changes else "nothing to do", changes)
    if failures:
        return SiteOutcome("failed" if stopped else "partial", f"{failures} step(s) failed", changes, failures)
    if rec is not None and ctx.ids.get("snapshot"):
        rec["snapshot"] = ctx.ids["snapshot"]
    done = "deleted" if ctx.mode == "delete" else "applied"
    what = f"{changes} object(s) deleted" if ctx.mode == "delete" else f"{changes} change(s) applied"
    return SiteOutcome(done if changes else "in-sync", what if changes else "nothing to do", changes)


def _record_created(rec, kind, oid):
    if kind and oid:
        rec["created"].setdefault(kind, []).append(str(oid))


# ── verify ──────────────────────────────────────────────────────────────────
def _member_checks(ctx, scope_id):
    s, api = ctx.site, ctx.api
    results = []
    for row in api.scopes_for(s.network):
        if str(row.get("dhcpscope_id")) == str(scope_id):
            continue
        label = row.get("dhcp_name", "member")
        problems = []
        rr = api.ranges_of(row["dhcpscope_id"])
        if not any(r.get("dhcprange_start_addr") == s.start and r.get("dhcprange_end_addr") == s.end for r in rr):
            problems.append("range missing")
        have = api.options_of(row["dhcpscope_id"])
        for lab, names, value in wanted_options(ctx):
            cur = next((have[n] for n in names if n in have), None)
            if cur is None:
                problems.append(f"{lab} option missing")
            elif norm_opt(cur.get("dhcpoption_value")) != norm_opt(value):
                problems.append(f"{lab} option differs")
        results.append((label, problems))
    return results


def verify(ctx):
    """Compare the site with the server; apply nothing. Returns SiteOutcome."""
    ctx.mode = "update"
    plan = plan_site(ctx)
    bad = 0
    for st in plan.steps:
        if st.action == "ok":
            ui.step(st.area, "ok", st.text)
        elif st.action in ("create", "update"):
            bad += 1
            ui.step(st.area, "fail", ("missing: " if st.action == "create" else "differs: ") + st.text)
        else:
            ui.step(st.area, "warn", st.text)
    scope_id = ctx.ids.get("scope")
    if scope_id and "dhcp" in ctx.parts:
        members = _member_checks(ctx, scope_id)
        if not members:
            ui.step("MEMBER", "warn", "no member scopes found yet (waiting for sync?)")
        for label, problems in members:
            if problems:
                ui.step("MEMBER", "warn", f"{label}: {', '.join(problems)} (may still be syncing)")
            else:
                ui.step("MEMBER", "ok", f"{label}: scope, range and options present")
    if bad:
        return SiteOutcome("mismatch", f"{bad} difference(s)", failures=bad)
    return SiteOutcome("verified", "matches the CSV")
