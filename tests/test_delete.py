import json
import os

import pytest

from conftest import HEADER, WM, WIFI
from eipdhcp import cli, ui
from eipdhcp.api import Api
from eipdhcp.app import App
from eipdhcp.ledger import latest
from eipdhcp.menu import run_menu
from fake_server import FakeSDS

PHRASE1 = "DELETE 1"


def make_app(settings, api, csv_path, **kw):
    return App(settings, api, csv_path, **kw)


def built(settings, api, csv_path):
    make_app(settings, api, csv_path, yes=True).run("create", dry=False)


@pytest.fixture
def typed(monkeypatch):
    """Answer the typed DELETE prompt."""
    def set_answer(text):
        monkeypatch.setattr(ui, "ask", lambda prompt: text)
    set_answer(PHRASE1)
    return set_answer


def deleted_ids(fake):
    return [c[1]["subnet_id"] for c in fake.calls if c[0] == "ip_subnet_delete"]


def test_dry_run_deletes_nothing(settings, api, fake, csv_file, capsys):
    built(settings, api, csv_file(WM))
    capsys.readouterr()
    s = make_app(settings, api, csv_file(WM)).run("delete", dry=True)
    out = capsys.readouterr().out
    assert s.count("planned") == 1 and not deleted_ids(fake) and fake.subnets and fake.scopes
    assert "(delete) network 10.1.18.0/23" in out and "(delete) block 10.1.18.0/23" in out
    assert "no active or static leases" in out and "(snapshot)" in out


def test_wet_delete_removes_network_block_and_everything_that_follows(settings, api, fake, csv_file, typed):
    built(settings, api, csv_file(WM))
    net_id = [x for x in fake.subnets if x["is_terminal"] == "1"][0]["subnet_id"]
    block_id = [x for x in fake.subnets if x["is_terminal"] == "0"][0]["subnet_id"]
    s = make_app(settings, api, csv_file(WM)).run("delete", dry=False)
    assert s.count("deleted") == 1 and s.exit_code == 0
    assert not fake.subnets and not fake.pools and not fake.scopes and not fake.ranges and not fake.options
    assert deleted_ids(fake) == [net_id, block_id]                      # network first, then the block


def test_snapshot_and_ledger_record_what_was_deleted(settings, api, fake, csv_file, typed):
    built(settings, api, csv_file(WM))
    make_app(settings, api, csv_file(WM)).run("delete", dry=False)
    rec = json.load(open(latest(settings.runs_dir)))["sites"][0]
    assert rec["status"] == "deleted" and len(rec["deleted"]) == 2
    snap = json.load(open(rec["snapshot"]))
    assert snap["network_row"]["subnet_name"] == "Site A" and snap["block_row"]
    assert len(snap["scopes"]) == 3 and len(snap["pools"]) == 1                 # smart + two members
    smart = [x for x in snap["scopes"] if x["scope"]["dhcp_id"] == "2"][0]
    assert smart["ranges"] and len(smart["options"]) == 2


def test_snapshot_is_taken_before_anything_is_deleted(settings, api, fake, csv_file, typed):
    built(settings, api, csv_file(WM))
    make_app(settings, api, csv_file(WM)).run("delete", dry=False)
    order = [c[0] for c in fake.calls if c[0] in ("dhcp_scope_options_list", "ip_subnet_delete")]
    assert order.index("dhcp_scope_options_list") < order.index("ip_subnet_delete")


def test_refuses_when_leases_are_active(settings, api, fake, csv_file, typed, capsys):
    built(settings, api, csv_file(WM))
    for r in fake.ranges:
        r["dhcprange_lease_count"] = "5"
    s = make_app(settings, api, csv_file(WM)).run("delete", dry=False)
    out = capsys.readouterr().out
    assert s.count("refused") == 1 and s.exit_code == 1 and not deleted_ids(fake)
    assert "5 on smart.dhcp.example, 5 on fo1.dhcp, 5 on member2.dhcp" in out and "Completed with problems" in out


def test_refuses_when_static_leases_exist(settings, api, fake, csv_file, typed, capsys):
    built(settings, api, csv_file(WM))
    smart = [x for x in fake.scopes if x["dhcp_id"] == "2"][0]
    fake.statics.append({"dhcpscope_id": smart["dhcpscope_id"], "dhcphost_name": "printer"})
    s = make_app(settings, api, csv_file(WM)).run("delete", dry=False)
    assert s.count("refused") == 1 and not deleted_ids(fake) and "static DHCP lease" in capsys.readouterr().out


def test_the_gateway_address_alone_does_not_block_a_delete(settings, api, fake, csv_file, typed, capsys):
    built(settings, api, csv_file(WM))
    assert [x["subnet_ip_used_size"] for x in fake.subnets if x["is_terminal"] == "1"] == ["1"]      # the gateway
    s = make_app(settings, api, csv_file(WM)).run("delete", dry=False)
    assert s.count("deleted") == 1
    assert "only the gateway address is registered in IPAM" in capsys.readouterr().out


def test_other_registered_addresses_block_a_delete_unless_allowed(settings, api, fake, csv_file, typed, capsys):
    built(settings, api, csv_file(WM))
    net = [x for x in fake.subnets if x["is_terminal"] == "1"][0]
    net["subnet_ip_used_size"] = "3"
    for ip in ("10.1.18.50", "10.1.18.51"):
        fake.addresses.append({"hostaddr": ip, "subnet_id": net["subnet_id"], "name": "pc"})
    s = make_app(settings, api, csv_file(WM)).run("delete", dry=False)
    assert s.count("refused") == 1 and not deleted_ids(fake)
    assert "2 IP address(es) in use in IPAM besides the gateway: 10.1.18.50, 10.1.18.51" in capsys.readouterr().out
    app = make_app(settings, api, csv_file(WM))
    app.allow_used = True
    assert app.run("delete", dry=False).count("deleted") == 1


def test_when_addresses_cannot_be_listed_one_used_address_is_taken_to_be_the_gateway(settings, api, fake, csv_file, typed, capsys):
    built(settings, api, csv_file(WM))
    fake.fail["ip_address_list"] = (400, {"errno": "1", "errmsg": "no"})
    assert make_app(settings, api, csv_file(WM)).run("delete", dry=True).count("planned") == 1
    assert "taken to be the gateway" in capsys.readouterr().out
    [x for x in fake.subnets if x["is_terminal"] == "1"][0]["subnet_ip_used_size"] = "12"
    s = make_app(settings, api, csv_file(WM)).run("delete", dry=False)
    assert s.count("refused") == 1 and "12 IP address(es) are in use in IPAM" in capsys.readouterr().out


def test_refuses_when_the_name_differs_unless_any_name(settings, api, fake, csv_file, typed, capsys):
    built(settings, api, csv_file(WM))
    other = WM.replace("Site A", "Some Other Site")
    s = make_app(settings, api, csv_file(other)).run("delete", dry=False)
    assert s.count("refused") == 1 and not deleted_ids(fake)
    assert "named 'Site A' but the CSV says 'Some Other Site'" in capsys.readouterr().out
    app = make_app(settings, api, csv_file(other))
    app.any_name = True
    assert app.run("delete", dry=False).count("deleted") == 1


def test_a_refused_site_does_not_stop_the_others(settings, api, fake, csv_file, typed):
    typed("DELETE 2")
    built(settings, api, csv_file(WM, WIFI))
    for r in fake.ranges:
        if r["dhcprange_start_addr"] == "10.2.4.51":
            r["dhcprange_lease_count"] = "3"
    s = make_app(settings, api, csv_file(WM, WIFI)).run("delete", dry=False)
    assert s.count("deleted") == 1 and s.count("refused") == 1
    assert {x["subnet_name"] for x in fake.subnets} == {"Site WiFi"}


def test_a_larger_parent_block_is_never_deleted(settings, api, fake, csv_file, typed, capsys):
    fake.subnets.append({"subnet_id": "50", "site_id": "2", "start_hostaddr": "10.1.0.0", "subnet_size": "65536",
                         "is_terminal": "0", "subnet_name": "big", "subnet_class_parameters": "", "subnet_ip_used_size": "0"})
    built(settings, api, csv_file(WM))
    s = make_app(settings, api, csv_file(WM)).run("delete", dry=False)
    assert s.count("deleted") == 1 and [x["subnet_id"] for x in fake.subnets] == ["50"]
    assert "is larger than the network: kept" in capsys.readouterr().out


def test_a_same_size_block_with_another_name_is_kept(settings, api, fake, csv_file, typed, capsys):
    built(settings, api, csv_file(WM))
    [x for x in fake.subnets if x["is_terminal"] == "0"][0]["subnet_name"] = "Shared block"
    s = make_app(settings, api, csv_file(WM)).run("delete", dry=False)
    assert s.count("deleted") == 1 and len(fake.subnets) == 1 and fake.subnets[0]["is_terminal"] == "0"
    assert "named 'Shared block'" in capsys.readouterr().out


def test_leftover_dhcp_objects_are_reported_not_hidden(settings, api, fake, csv_file, typed, capsys):
    built(settings, api, csv_file(WM))
    fake.cascade = False                                                   # the DHCP side does not follow
    s = make_app(settings, api, csv_file(WM)).run("delete", dry=False)
    out = capsys.readouterr().out
    assert s.count("partial") == 1 and s.exit_code == 1
    assert "still present: 3 DHCP scope(s)" in out and "DHCP range(s)" in out
    assert not fake.subnets and fake.scopes                                 # the tool did not delete DHCP objects itself


def test_nothing_to_delete(settings, api, fake, csv_file, typed, capsys):
    s = make_app(settings, api, csv_file(WM)).run("delete", dry=False)
    assert s.count("skipped") == 1 and s.exit_code == 0 and "nothing to delete" in capsys.readouterr().out


def test_dhcp_scope_without_ipam_network_is_left_for_the_gui(settings, api, fake, csv_file, typed, capsys):
    app = make_app(settings, api, csv_file(WM), yes=True)
    app.parts = frozenset({"dhcp"})
    app.run("create", dry=False)
    capsys.readouterr()
    s = make_app(settings, api, csv_file(WM)).run("delete", dry=False)
    assert s.count("refused") == 1 and fake.scopes and not deleted_ids(fake)
    assert "remove the scope in the GUI" in capsys.readouterr().out


# ── confirmation ────────────────────────────────────────────────────────────────────────
def test_wrong_phrase_aborts_even_with_yes(settings, api, fake, csv_file, monkeypatch):
    built(settings, api, csv_file(WM))
    monkeypatch.setattr(ui, "ask", lambda prompt: "yes")
    s = make_app(settings, api, csv_file(WM), yes=True).run("delete", dry=False)
    assert s.aborted and not deleted_ids(fake) and fake.subnets


def test_phrase_contains_the_number_of_sites(settings, api, fake, csv_file, monkeypatch):
    built(settings, api, csv_file(WM, WIFI))
    monkeypatch.setattr(ui, "ask", lambda prompt: "DELETE 1")
    assert make_app(settings, api, csv_file(WM, WIFI)).run("delete", dry=False).aborted
    monkeypatch.setattr(ui, "ask", lambda prompt: "DELETE 2")
    s = make_app(settings, api, csv_file(WM, WIFI)).run("delete", dry=False)
    assert s.count("deleted") == 2 and not fake.subnets


def test_site_filter_limits_what_is_deleted(settings, api, fake, csv_file, typed):
    built(settings, api, csv_file(WM, WIFI))
    s = make_app(settings, api, csv_file(WM, WIFI), site_filter=["Site WiFi"]).run("delete", dry=False)
    assert s.count("deleted") == 1
    assert {x["subnet_name"] for x in fake.subnets} == {"Site A"}


def test_delete_needs_only_space_site_and_network(settings, api, fake, csv_file, typed):
    built(settings, api, csv_file(WM))
    minimal = "TestSpace,Site A,10.1.18.0/23,,,,,,"
    s = make_app(settings, api, csv_file(minimal)).run("delete", dry=False)
    assert s.count("deleted") == 1 and not fake.subnets


def test_invalid_rows_are_not_sent_for_delete(settings, api, fake, csv_file, typed):
    built(settings, api, csv_file(WM))
    s = make_app(settings, api, csv_file("TestSpace,Bad,10.1.18.5/23,,,,,,", WM)).run("delete", dry=False)
    assert s.count("invalid") == 1 and s.count("deleted") == 1


def test_create_after_delete_works_again(settings, api, fake, csv_file, typed):
    built(settings, api, csv_file(WM))
    make_app(settings, api, csv_file(WM)).run("delete", dry=False)
    built(settings, api, csv_file(WM))
    assert len(fake.subnets) == 2 and len([x for x in fake.scopes if x["dhcp_id"] == "2"]) == 1


# ── cli / menu ───────────────────────────────────────────────────────────────────────────
@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "BASE_DIR", str(tmp_path))
    for k, v in (("EIP_HOST", "h"), ("EIP_USER", "u"), ("EIP_PASSWORD", "p")):
        monkeypatch.setenv(k, v)
    csv_path = tmp_path / "s.csv"
    csv_path.write_text(HEADER + WM + "\n", encoding="utf-8")
    return FakeSDS(), str(csv_path)


def run_cli(fake, *argv):
    return cli.main(list(argv), api_factory=lambda settings: Api(fake, sleep=lambda s: None))


def test_cli_delete_needs_a_mode(cli_env):
    fake, csv_path = cli_env
    with pytest.raises(SystemExit):
        run_cli(fake, csv_path, "--delete")


def test_cli_delete_dry_then_wet(cli_env, monkeypatch):
    fake, csv_path = cli_env
    run_cli(fake, csv_path, "--wet-run", "--yes")
    assert run_cli(fake, csv_path, "--delete", "--dry-run") == 0 and fake.subnets
    monkeypatch.setattr(ui, "ask", lambda prompt: PHRASE1)
    assert run_cli(fake, csv_path, "--delete", "--wet-run") == 0
    assert not fake.subnets and not fake.scopes


def test_cli_delete_is_not_skipped_by_yes(cli_env, monkeypatch):
    fake, csv_path = cli_env
    run_cli(fake, csv_path, "--wet-run", "--yes")
    monkeypatch.setattr(ui, "ask", lambda prompt: "nope")
    run_cli(fake, csv_path, "--delete", "--wet-run", "--yes")
    assert fake.subnets and fake.scopes


def test_menu_delete_previews_then_asks_the_typed_phrase(cli_env, monkeypatch, tmp_path, capsys):
    fake, csv_path = cli_env
    from eipdhcp.config import Settings
    st = Settings(host="h", logs_dir=str(tmp_path / "logs"), runs_dir=str(tmp_path / "runs"))
    app = App(st, Api(fake, sleep=lambda s: None), csv_path)
    answers = iter(["3", "YES", "", "d", PHRASE1, "", "0"])
    monkeypatch.setattr(ui, "ask", lambda prompt: next(answers))
    run_menu(app)
    out = capsys.readouterr().out
    assert not fake.subnets and not fake.scopes and "DELETE sites" in out and "✔ deleted" in out


def test_network_and_broadcast_addresses_do_not_block_a_delete(settings, api, fake, csv_file, typed, capsys):
    built(settings, api, csv_file(WM))
    net = [x for x in fake.subnets if x["is_terminal"] == "1"][0]
    net["subnet_ip_used_size"] = "3"
    for ip in ("10.1.18.0", "10.1.19.255"):          # WM is a /23
        fake.addresses.append({"hostaddr": ip, "subnet_id": net["subnet_id"], "name": ""})
    s = make_app(settings, api, csv_file(WM)).run("delete", dry=False)
    assert s.count("deleted") == 1
    assert "network/broadcast addresses are registered" in capsys.readouterr().out


def test_menu_offers_to_override_the_in_use_refusal(cli_env, monkeypatch, tmp_path, capsys):
    fake, csv_path = cli_env
    from eipdhcp.config import Settings
    st = Settings(host="h", logs_dir=str(tmp_path / "logs"), runs_dir=str(tmp_path / "runs"))
    app = App(st, Api(fake, sleep=lambda s: None), csv_path)
    app.run("create", dry=False, yes=True)
    net = [x for x in fake.subnets if x["is_terminal"] == "1"][0]
    net["subnet_ip_used_size"] = "3"
    for ip in ("10.1.18.50", "10.1.18.51"):
        fake.addresses.append({"hostaddr": ip, "subnet_id": net["subnet_id"], "name": "pc"})
    # d -> refused -> YES to override -> typed DELETE phrase
    answers = iter(["d", "YES", PHRASE1, "", "0"])
    monkeypatch.setattr(ui, "ask", lambda prompt: next(answers))
    run_menu(app)
    out = capsys.readouterr().out
    assert "refused because IPAM shows addresses in use" in out and "✔ deleted" in out
    assert not fake.subnets and app.allow_used is False and app.site_filter == []


def test_menu_override_can_be_declined(cli_env, monkeypatch, tmp_path, capsys):
    fake, csv_path = cli_env
    from eipdhcp.config import Settings
    st = Settings(host="h", logs_dir=str(tmp_path / "logs"), runs_dir=str(tmp_path / "runs"))
    app = App(st, Api(fake, sleep=lambda s: None), csv_path)
    app.run("create", dry=False, yes=True)
    net = [x for x in fake.subnets if x["is_terminal"] == "1"][0]
    net["subnet_ip_used_size"] = "2"
    fake.addresses.append({"hostaddr": "10.1.18.50", "subnet_id": net["subnet_id"], "name": "pc"})
    answers = iter(["d", "", "", "0"])
    monkeypatch.setattr(ui, "ask", lambda prompt: next(answers))
    run_menu(app)
    assert fake.subnets and "Skipped." in capsys.readouterr().out
