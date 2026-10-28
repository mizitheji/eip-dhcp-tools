import ipaddress

from conftest import WM, WIFI
from eipdhcp import validate as val
from eipdhcp.config import Settings
from eipdhcp.rows import build_site, build_sites, filter_sites, read_csv, split_list


def sites_from(csv_file, *rows):
    _, parsed = read_csv(csv_file(*rows))
    return build_sites(parsed, Settings())


def test_split_list_accepts_commas_semicolons_spaces():
    assert split_list("1.1.1.1, 2.2.2.2;3.3.3.3 4.4.4.4") == ["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4"]


def test_numbers_are_offsets_from_the_network_address(csv_file):
    s = sites_from(csv_file, 'TestSpace,X,10.1.18.0/23,smart.dhcp.example,10,510,4,"9.9.9.9",a.dhcp')[0]
    assert (s.start, s.end, s.gateway, s.dns) == ("10.1.18.10", "10.1.19.254", "10.1.18.4", ["9.9.9.9"])


def test_full_ips_are_used_as_they_are(csv_file):
    s = sites_from(csv_file, WIFI)[0]
    assert (s.start, s.end, s.gateway) == ("10.2.4.51", "10.2.4.244", "10.2.4.3")
    assert s.dns == ["10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5"] and not s.problems


def test_nothing_is_filled_in_for_you(csv_file):
    s = sites_from(csv_file, 'TestSpace,Bare,10.2.4.0/24,smart.dhcp.example,,,,,fo1.dhcp')[0]
    assert (s.start, s.end, s.gateway, s.dns) == ("", "", "", [])
    errs = [i.msg for i in val.validate_offline([s]) if i.level == val.ERROR]
    assert any("start_address / end_address are missing" in m for m in errs)


def test_old_csvs_with_extra_columns_still_work(tmp_path):
    p = tmp_path / "old.csv"
    p.write_text("space,site,type,network,dhcp_server,start_address,end_address,gateway,dns_servers,failover_channel\n"
                 "TestSpace,Old,wifi,10.2.4.0/24,smart.dhcp.example,10,200,10.2.4.1,1.1.1.1,a.dhcp\n", encoding="utf-8")
    _, rows = read_csv(str(p))
    s = build_sites(rows, Settings())[0]
    assert (s.start, s.end, s.gateway, s.problems) == ("10.2.4.10", "10.2.4.200", "10.2.4.1", [])


def test_bad_network_is_a_problem(csv_file):
    (b,) = sites_from(csv_file, 'TestSpace,B,10.2.4.5/24,smart.dhcp.example,1,2,,,')
    assert any("network" in p for p in b.problems)


def test_filter_sites_is_case_insensitive(csv_file):
    sites = sites_from(csv_file, WM, WIFI)
    sel, unknown = filter_sites(sites, ["site a", "Nope"])
    assert [s.name for s in sel] == ["Site A"] and unknown == ["nope"]


def msgs(issues, level):
    return [i.msg for i in issues if i.level == level]


def test_offline_validation_catches_mistakes(csv_file):
    sites = sites_from(
        csv_file,
        'TestSpace,Dup1,10.2.1.0/24,s.dhcp,10,200,10.2.1.1,"1.1.1.1",f.dhcp',
        'TestSpace,Dup2,10.2.1.0/24,s.dhcp,10,200,10.2.1.1,"1.1.1.1",f.dhcp',
        'TestSpace,Overlap,10.2.0.0/16,s.dhcp,10,200,10.2.0.1,"1.1.1.1",f.dhcp',
        'TestSpace,Outside,10.2.2.0/24,s.dhcp,10,300,10.2.2.1,"1.1.1.1",f.dhcp',
        'TestSpace,Reversed,10.2.3.0/24,s.dhcp,200,10,10.2.3.1,"1.1.1.1",f.dhcp',
        'TestSpace,GwIn,10.2.5.0/24,s.dhcp,10,200,10.2.5.50,"1.1.1.1",f.dhcp',
        'TestSpace,BadDns,10.2.6.0/24,s.dhcp,10,200,10.2.6.1,"1.1.1",f.dhcp',
        'TestSpace,Broadcast,10.2.7.0/24,s.dhcp,10,255,10.2.7.1,"1.1.1.1",f.dhcp',
        'TestSpace,NoGwDns,10.2.8.0/24,s.dhcp,10,200,,,')
    issues = val.validate_offline(sites)
    errs, warns = msgs(issues, val.ERROR), msgs(issues, val.WARN)
    assert any("already listed on line 2" in m for m in errs)
    assert any("overlaps 10.2.1.0/24" in m for m in errs)
    assert any("outside 10.2.2.0/24" in m for m in errs)
    assert any("after end" in m for m in errs)
    assert any("not a valid IP" in m for m in errs)
    assert any("network/broadcast" in m for m in errs)
    assert any("inside the DHCP range" in m for m in warns)
    assert any("no gateway" in m for m in warns) and any("no DNS" in m for m in warns)


def test_clean_csv_has_no_errors(csv_file):
    assert not msgs(val.validate_offline(sites_from(csv_file, WM, WIFI)), val.ERROR)


def test_online_validation(csv_file, api, fake):
    for sid, addr in (("1", "10.3.0.0"), ("3", "10.3.8.0")):
        fake.subnets.append({"subnet_id": sid, "site_id": "2", "start_hostaddr": addr, "subnet_size": "256",
                             "is_terminal": "1", "subnet_name": "x", "subnet_class_parameters": ""})
    sites = sites_from(
        csv_file,
        'TestSpace,GoodFailover,10.2.4.0/24,smart.dhcp.example,10,200,10.2.4.1,"1.1.1.1",fo1.dhcp',
        'TestSpace,BadFailover,10.2.5.0/24,smart.dhcp.example,10,200,10.2.5.1,"1.1.1.1",ghost.dhcp',
        'NoSpace,BadSpace,10.2.6.0/24,smart.dhcp.example,10,200,10.2.6.1,"1.1.1.1",fo1.dhcp',
        'TestSpace,BadServer,10.2.7.0/24,nope.dhcp,10,200,10.2.7.1,"1.1.1.1",fo1.dhcp',
        'TestSpace,Overlaps,10.3.8.0/23,smart.dhcp.example,10,200,10.3.8.1,"1.1.1.1",fo1.dhcp',
        'TestSpace,Existing,10.3.0.0/24,smart.dhcp.example,10,200,10.3.0.1,"1.1.1.1",fo1.dhcp')
    issues = val.validate(sites, api)
    by_site = {}
    for i in issues:
        by_site.setdefault(i.site, []).append(i)
    assert not [i for i in by_site.get("GoodFailover", []) if i.level == val.ERROR]
    assert any("ghost.dhcp" in i.msg for i in by_site["BadFailover"])
    assert any("TestSpace" not in i.msg and "NoSpace" in i.msg for i in by_site["BadSpace"])
    assert any("nope.dhcp" in i.msg for i in by_site["BadServer"])
    assert any("overlaps existing IPAM network" in i.msg for i in by_site["Overlaps"])
    assert any(i.level == val.INFO and "already in IPAM" in i.msg for i in by_site["Existing"])
