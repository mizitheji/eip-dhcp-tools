import json
import os
from urllib.parse import parse_qsl

import pytest

from conftest import WM, WIFI
from eipdhcp import ui
from eipdhcp.app import App
from eipdhcp.ledger import latest


def make_app(settings, api, csv_path, **kw):
    return App(settings, api, csv_path, **kw)


def only_row(rows, **match):
    found = [r for r in rows if all(r.get(k) == v for k, v in match.items())]
    assert len(found) == 1, found
    return found[0]


# ── create ───────────────────────────────────────────────────────────────────────
def test_create_dry_run_changes_nothing(settings, api, fake, csv_file, capsys):
    s = make_app(settings, api, csv_file(WM, WIFI)).run("create", dry=True)
    out = capsys.readouterr().out
    assert s.count("planned") == 2 and s.exit_code == 0
    assert fake.writes() == [] and not fake.subnets and not fake.scopes
    assert "block 10.1.18.0/23" in out and "DRY" in out and "Nothing was changed" in out
    assert not os.path.isdir(settings.runs_dir) or not [f for f in os.listdir(settings.runs_dir) if f.endswith(".json")]


def test_create_wet_builds_everything(settings, api, fake, csv_file):
    s = make_app(settings, api, csv_file(WM), yes=True).run("create", dry=False)
    assert s.count("applied") == 1 and s.exit_code == 0
    block = only_row(fake.subnets, is_terminal="0")
    net = only_row(fake.subnets, is_terminal="1")
    assert block["subnet_name"] == net["subnet_name"] == "Site A"
    cp = dict(parse_qsl(net["subnet_class_parameters"]))
    assert cp["gateway"] == "10.1.18.4" and cp["dhcp_failover_name"] == "fo1.dhcp"
    smart = only_row(fake.scopes, dhcp_id="2")
    assert smart["dhcpfailover_name"] == "fo1.dhcp" and len(fake.scopes) == 3          # smart + 2 members
    rng = only_row(fake.ranges, dhcpscope_id=smart["dhcpscope_id"])
    assert (rng["dhcprange_start_addr"], rng["dhcprange_end_addr"]) == ("10.1.18.10", "10.1.19.254")
    pool = only_row(fake.pools)
    assert pool["pool_read_only"] == "1" and "dhcprange=1" in pool["pool_class_parameters"]
    opts = {o["dhcpoption_name"]: o["dhcpoption_value"] for o in fake.options if o["dhcpscope_id"] == smart["dhcpscope_id"]}
    assert opts == {"option routers": "10.1.18.4;", "option domain-name-servers": "10.0.0.2,10.0.0.3;"}
    assert len(fake.options) == 6                                                           # replicated to both members


def test_create_writes_a_ledger_with_ids(settings, api, fake, csv_file):
    make_app(settings, api, csv_file(WM), yes=True).run("create", dry=False)
    data = json.load(open(latest(settings.runs_dir)))
    rec = data["sites"][0]
    assert rec["status"] == "applied" and rec["network"] == "10.1.18.0/23"
    created = rec["created"]
    assert set(created) == {"block", "network", "scope", "range", "pool", "option"} and len(created["option"]) == 2
    assert created["network"][0] == only_row(fake.subnets, is_terminal="1")["subnet_id"]


def test_create_skips_sites_that_exist_and_touches_nothing(settings, api, fake, csv_file, capsys):
    app = make_app(settings, api, csv_file(WM), yes=True)
    app.run("create", dry=False)
    before = len(fake.writes())
    capsys.readouterr()
    s = app.run("create", dry=False)
    assert s.count("skipped") == 1 and len(fake.writes()) == before
    assert "scope already exists" in capsys.readouterr().out


def test_existing_parent_block_is_reused(settings, api, fake, csv_file):
    fake.subnets.append({"subnet_id": "50", "site_id": "2", "start_hostaddr": "10.1.0.0", "subnet_size": "65536",
                         "is_terminal": "0", "subnet_name": "big", "subnet_class_parameters": ""})
    make_app(settings, api, csv_file(WM), yes=True).run("create", dry=False)
    assert len([s for s in fake.subnets if s["is_terminal"] == "0"]) == 1               # no extra block
    assert fake.names("ip_subnet_create")[0]["parent_subnet_id"] == "50"


def test_block_list_is_loaded_once_for_many_rows(settings, api, fake, csv_file):
    rows = [f'TestSpace,S{i},10.2.{i}.0/24,smart.dhcp.example,10,200,10.2.{i}.1,"1.1.1.1",fo1.dhcp' for i in range(1, 6)]
    make_app(settings, api, csv_file(*rows), yes=True).run("create", dry=False)
    block_lists = [c for c in fake.calls if c[0] == "ip_subnet_list" and "is_terminal='0'" in c[1].get("WHERE", "")]
    assert len(block_lists) == 1                                                        # cached per space


def test_pool_falls_back_to_plain_variant(settings, api, fake, csv_file):
    fake.reject_pool_dhcprange = True
    s = make_app(settings, api, csv_file(WM), yes=True).run("create", dry=False)
    assert s.count("applied") == 1 and "dhcprange" not in only_row(fake.pools)["pool_class_parameters"]


def test_critical_failure_stops_the_site(settings, api, fake, csv_file):
    fake.fail["dhcp_scope_create"] = (400, {"errno": "1", "errmsg": "boom"})
    s = make_app(settings, api, csv_file(WM, WIFI), yes=True).run("create", dry=False)
    assert s.count("failed") == 2 and s.exit_code == 1
    assert not fake.ranges and not fake.pools and not fake.options                       # nothing after the failed step


def test_pool_failure_is_partial_and_options_still_run(settings, api, fake, csv_file):
    fake.fail["ip_pool_add"] = (400, {"errno": "1", "errmsg": "pool boom"})
    s = make_app(settings, api, csv_file(WM), yes=True).run("create", dry=False)
    assert s.count("partial") == 1 and s.exit_code == 1 and fake.options and fake.ranges


def test_invalid_rows_are_not_sent(settings, api, fake, csv_file):
    bad = 'TestSpace,Bad,10.2.9.0/24,smart.dhcp.example,10,999,10.2.9.1,"1.1.1.1",fo1.dhcp'
    s = make_app(settings, api, csv_file(bad, WM), yes=True).run("create", dry=False)
    assert s.count("invalid") == 1 and s.count("applied") == 1
    assert not [n for n in fake.names("ip_subnet_create") if n["subnet_addr"] == "10.2.9.0"]


def test_console_has_no_traceback_but_the_log_does(settings, api, fake, csv_file, capsys):
    path = ui.setup_logging("t", settings.logs_dir)
    fake.fail["dhcp_scope_create"] = (400, {"errno": "1", "errmsg": "boom"})
    api.sds.query = (lambda orig: (lambda svc, *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom")) if svc == "ip_pool_add" else orig(svc, *a, **k)))(api.sds.query)
    make_app(settings, api, csv_file(WM), yes=True).run("create", dry=False)
    assert "Traceback" not in capsys.readouterr().out
    assert os.path.getsize(path) > 0


def test_parts_flags_limit_what_is_created(settings, api, fake, csv_file):
    app = make_app(settings, api, csv_file(WM), yes=True)
    app.parts = frozenset({"dhcp", "options"})                                          # like --no-ipam --skip-pool
    app.run("create", dry=False)
    assert not fake.subnets and not fake.pools and fake.scopes and fake.options


def test_options_only_adds_missing_options_to_an_existing_scope(settings, api, fake, csv_file):
    app = make_app(settings, api, csv_file(WM), yes=True)
    app.parts = frozenset({"dhcp", "ipam", "pool"})
    app.run("create", dry=False)
    assert not fake.options
    app.parts = frozenset({"options"})
    s = app.run("create", dry=False)
    assert s.count("applied") == 1 and len(fake.options) == 6


def test_batch_pauses_and_can_stop(settings, api, fake, csv_file, monkeypatch):
    rows = [f'TestSpace,S{i},10.2.{i}.0/24,smart.dhcp.example,10,200,10.2.{i}.1,"1.1.1.1",fo1.dhcp' for i in range(1, 4)]
    answers = iter(["n"])
    monkeypatch.setattr(ui, "ask", lambda prompt: next(answers))
    app = make_app(settings, api, csv_file(*rows), batch=2)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    s = app.run("create", dry=False)
    assert s.aborted and len(s.results) == 2 and len([x for x in fake.scopes if x["dhcp_id"] == "2"]) == 2


def test_wet_run_asks_for_confirmation(settings, api, fake, csv_file, monkeypatch):
    monkeypatch.setattr(ui, "ask", lambda prompt: "yes")                                # lowercase is not enough
    s = make_app(settings, api, csv_file(WM)).run("create", dry=False)
    assert s.aborted and not fake.writes()


# ── update / verify ──────────────────────────────────────────────────────────────────
def built(settings, api, csv_path):
    make_app(settings, api, csv_path, yes=True).run("create", dry=False)


def test_verify_passes_after_create(settings, api, fake, csv_file, capsys):
    built(settings, api, csv_file(WM, WIFI))
    capsys.readouterr()
    s = make_app(settings, api, csv_file(WM, WIFI)).run("verify", dry=True)
    out = capsys.readouterr().out
    assert s.count("verified") == 2 and s.exit_code == 0 and "MEMBER" in out and "✘" not in out


def test_verify_reports_differences_and_missing_pieces(settings, api, fake, csv_file, capsys):
    built(settings, api, csv_file(WM))
    fake.pools.clear()                                                                   # someone deleted the pool
    fake.options[:] = [o for o in fake.options if o["dhcpoption_name"] != "option domain-name-servers"]
    capsys.readouterr()
    s = make_app(settings, api, csv_file(WM)).run("verify", dry=True)
    out = capsys.readouterr().out
    assert s.count("mismatch") == 1 and s.exit_code == 1
    assert "missing: DHCP 10.1.18.10" in out and "missing: dns" in out


def test_update_dry_run_shows_diff_and_changes_nothing(settings, api, fake, csv_file, capsys):
    built(settings, api, csv_file(WM))
    changed = 'TestSpace,Site A,10.1.18.0/23,smart.dhcp.example,10.1.18.10,10.1.19.250,10.1.18.5,"10.0.0.2",fo1.dhcp'
    writes_before, capsys_ = len(fake.writes()), capsys.readouterr()
    s = make_app(settings, api, csv_file(changed)).run("update", dry=True)
    out = capsys.readouterr().out
    assert s.count("planned") == 1 and len(fake.writes()) == writes_before
    assert "10.1.19.254 → 10.1.18.10 – 10.1.19.250" in out or "→ 10.1.18.10 – 10.1.19.250" in out
    assert "gateway: 10.1.18.4 → 10.1.18.5" in out and "dns: 10.0.0.2,10.0.0.3 → 10.0.0.2" in out


def test_update_wet_applies_every_difference_and_verifies_clean(settings, api, fake, csv_file):
    built(settings, api, csv_file(WM))
    changed = 'TestSpace,Site A,10.1.18.0/23,smart.dhcp.example,10.1.18.10,10.1.19.250,10.1.18.5,"10.0.0.2",member2.dhcp'
    s = make_app(settings, api, csv_file(changed), yes=True).run("update", dry=False)
    assert s.count("applied") == 1 and s.exit_code == 0
    net = only_row(fake.subnets, is_terminal="1")
    cp = dict(parse_qsl(net["subnet_class_parameters"]))
    assert cp["gateway"] == "10.1.18.5" and cp["dhcp_failover_name"] == "member2.dhcp"
    assert cp["dns_update"] == "0"                                                       # untouched keys survive (merge, not replace)
    assert {r["dhcprange_end_addr"] for r in fake.ranges} == {"10.1.19.250"}           # smart range and member copies
    assert {r["dhcprange_failover_name"] for r in fake.ranges} == {"member2.dhcp"}
    assert only_row(fake.pools)["end_hostaddr"] == "10.1.19.250"
    assert {o["dhcpoption_value"] for o in fake.options if o["dhcpoption_name"] == "option routers"} == {"10.1.18.5;"}
    assert {o["dhcpoption_value"] for o in fake.options if o["dhcpoption_name"] == "option domain-name-servers"} == {"10.0.0.2;"}
    again = make_app(settings, api, csv_file(changed)).run("verify", dry=True)
    assert again.count("verified") == 1


def test_update_is_idempotent(settings, api, fake, csv_file):
    built(settings, api, csv_file(WM))
    before = len(fake.writes())
    s = make_app(settings, api, csv_file(WM), yes=True).run("update", dry=False)
    assert s.count("in-sync") == 1 and len(fake.writes()) == before


def test_update_recreates_missing_pool_and_options(settings, api, fake, csv_file):
    built(settings, api, csv_file(WM))
    fake.pools.clear()
    fake.options.clear()
    s = make_app(settings, api, csv_file(WM), yes=True).run("update", dry=False)
    assert s.count("applied") == 1 and len(fake.pools) == 1 and len(fake.options) == 6


def test_update_renames_unless_keep_names(settings, api, fake, csv_file):
    built(settings, api, csv_file(WM))
    renamed = WM.replace("Site A", "Site A Renamed")
    make_app(settings, api, csv_file(renamed), yes=True, keep_names=True).run("update", dry=False)
    assert only_row(fake.subnets, is_terminal="1")["subnet_name"] == "Site A"
    make_app(settings, api, csv_file(renamed), yes=True).run("update", dry=False)
    assert only_row(fake.subnets, is_terminal="1")["subnet_name"] == "Site A Renamed"
    assert {s["dhcpscope_name"] for s in fake.scopes} == {"Site A Renamed"}


def test_update_with_the_old_update_ipam_shape_only_touches_ipam(settings, api, fake, csv_file):
    built(settings, api, csv_file(WM))
    app = make_app(settings, api, csv_file(WM.replace("10.1.18.4", "10.1.18.9")), yes=True)
    app.parts = frozenset({"ipam"})
    app.run("update", dry=False)
    assert "gateway=10.1.18.9" in only_row(fake.subnets, is_terminal="1")["subnet_class_parameters"]
    assert fake.count("dhcp_option_update", "dhcp_range_update", "dhcp_scope_update") == 0


def test_update_creates_a_site_that_does_not_exist_yet(settings, api, fake, csv_file):
    s = make_app(settings, api, csv_file(WM), yes=True).run("update", dry=False)
    assert s.count("applied") == 1 and fake.scopes and fake.pools


# ── the real server makes the DHCP scope itself when the IPAM network has a failover channel ──
def test_create_works_when_the_server_already_made_the_scope(settings, api, fake, csv_file):
    fake.auto_scope = True
    s = make_app(settings, api, csv_file(WM), yes=True).run("create", dry=False)
    assert s.count("applied") == 1 and s.exit_code == 0
    assert fake.count("dhcp_scope_create") == 0                                          # not attempted again
    assert len([x for x in fake.scopes if x["dhcp_id"] == "2"]) == 1 and fake.ranges and fake.pools and len(fake.options) == 6
    rec = json.load(open(latest(settings.runs_dir)))["sites"][0]
    assert rec["created"]["scope"] == [only_row(fake.scopes, dhcp_id="2")["dhcpscope_id"]]
    assert make_app(settings, api, csv_file(WM)).run("verify", dry=True).count("verified") == 1


def test_a_router_option_made_by_the_server_is_not_added_twice(settings, api, fake, csv_file, capsys):
    fake.auto_scope = fake.auto_router = True
    s = make_app(settings, api, csv_file(WM), yes=True).run("create", dry=False)
    assert s.count("applied") == 1 and "router already set" in capsys.readouterr().out
    assert len([o for o in fake.options if o["dhcpoption_name"] == "option routers"]) == 3     # smart + 2 members, once each
    assert len([o for o in fake.options if o["dhcpoption_name"] == "option domain-name-servers"]) == 3


def test_the_create_steps_are_idempotent_when_the_object_is_already_there(settings, api, fake, csv_file):
    from eipdhcp import engine
    from eipdhcp.rows import build_sites, read_csv
    built(settings, api, csv_file(WM))
    site = build_sites(read_csv(csv_file(WM))[1])[0]
    smart = only_row(fake.scopes, dhcp_id="2")
    net = only_row(fake.subnets, is_terminal="1")
    ctx = engine.Ctx(api, settings, site, ids={"dhcp_id": "2", "scope": smart["dhcpscope_id"], "network": net["subnet_id"],
                                              "site_id": "2"})
    before = len(fake.writes())
    for run in (engine._create_scope, engine._create_range, engine._create_pool,
                engine._opt_add("router", settings.opt_router_names, "10.1.18.4")):
        out = run(ctx)
        assert out.ok and "already" in out.text, out
    assert len(fake.writes()) == before


def test_a_scope_that_still_fails_to_create_is_reported(settings, api, fake, csv_file):
    fake.fail["dhcp_scope_create"] = (400, {"errno": "1", "errmsg": "DHCP scope already exists"})
    s = make_app(settings, api, csv_file(WM), yes=True).run("create", dry=False)
    assert s.count("failed") == 1                                                          # a real error is still an error


def test_update_completes_a_site_left_half_built_by_the_first_failed_run(settings, api, fake, csv_file, capsys):
    """Exactly what the first real run left behind: block + network + a scope made by the server, nothing else."""
    fake.auto_scope = True
    fake.fail["dhcp_range_create"] = (400, {"errno": "1", "errmsg": "boom"})
    made = make_app(settings, api, csv_file(WM), yes=True).run("create", dry=False)
    assert made.count("failed") == 1 and fake.scopes and not fake.ranges and not fake.pools
    del fake.fail["dhcp_range_create"]
    again = make_app(settings, api, csv_file(WM), yes=True).run("create", dry=False)
    assert again.count("skipped") == 1                                                      # create never touches an existing site
    capsys.readouterr()
    s = make_app(settings, api, csv_file(WM), yes=True).run("update", dry=False)
    out = capsys.readouterr().out
    assert s.count("applied") == 1 and fake.ranges and fake.pools and len(fake.options) == 6
    assert "range created" in out and "scope in sync" in out and "network in sync" in out
    assert make_app(settings, api, csv_file(WM)).run("verify", dry=True).count("verified") == 1
