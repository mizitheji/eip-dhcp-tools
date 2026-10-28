import json
import os

import pytest

from conftest import HEADER, WM
from eipdhcp import cli, ui
from eipdhcp.api import Api
from eipdhcp.app import App
from eipdhcp.menu import run_menu
from fake_server import FakeSDS, Resp


# ── api retries ──────────────────────────────────────────────────────────────────────
class Flaky:
    def __init__(self, fails):
        self.fails, self.calls = fails, 0

    def query(self, service, params=None, option=False, timeout=None, allow_raw=False):
        self.calls += 1
        if self.calls <= self.fails:
            raise ConnectionError("timeout")
        return Resp(200, [{"ok": 1}])


def test_reads_are_retried():
    sds = Flaky(2)
    code, body = Api(sds, sleep=lambda s: None).call("ip_site_list")
    assert code == 200 and sds.calls == 3


def test_reads_give_up_after_three_tries():
    sds = Flaky(9)
    with pytest.raises(ConnectionError):
        Api(sds, sleep=lambda s: None).call("ip_site_list")
    assert sds.calls == 3


def test_writes_are_never_retried():
    sds = Flaky(9)
    with pytest.raises(ConnectionError):
        Api(sds, sleep=lambda s: None).call("dhcp_scope_create", {"x": 1})
    assert sds.calls == 1


# ── cli ──────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "BASE_DIR", str(tmp_path))
    for k, v in (("EIP_HOST", "h"), ("EIP_USER", "u"), ("EIP_PASSWORD", "p")):
        monkeypatch.setenv(k, v)
    fake = FakeSDS()
    csv_path = tmp_path / "s.csv"
    csv_path.write_text(HEADER + WM + "\n", encoding="utf-8")
    return fake, str(csv_path), tmp_path


def run_cli(fake, *argv):
    return cli.main(list(argv), api_factory=lambda settings: Api(fake, sleep=lambda s: None))


def test_cli_needs_a_mode_for_create(cli_env):
    fake, csv_path, _ = cli_env
    with pytest.raises(SystemExit):
        run_cli(fake, csv_path, "--create")


def test_cli_dry_run_then_wet_run(cli_env, capsys):
    fake, csv_path, tmp = cli_env
    assert run_cli(fake, csv_path, "--dry-run") == 0 and not fake.writes()
    assert run_cli(fake, csv_path, "--wet-run", "--yes") == 0 and fake.scopes
    assert [f for f in os.listdir(tmp / "logs")] and [f for f in os.listdir(tmp / "runs") if f.endswith(".json")]
    assert run_cli(fake, csv_path, "--verify") == 0


def test_cli_exit_code_is_nonzero_on_problems(cli_env):
    fake, csv_path, _ = cli_env
    fake.fail["dhcp_scope_create"] = (400, {"errno": "1", "errmsg": "boom"})
    assert run_cli(fake, csv_path, "--wet-run", "--yes") == 1


def test_cli_validate_and_offline_validate(cli_env):
    fake, csv_path, _ = cli_env
    assert run_cli(fake, csv_path, "--validate") == 0
    assert run_cli(None, csv_path, "--validate", "--offline") == 0


def test_cli_site_filter_and_update_ipam_alias(cli_env):
    fake, csv_path, _ = cli_env
    run_cli(fake, csv_path, "--wet-run", "--yes")
    n = len(fake.writes())
    assert run_cli(fake, csv_path, "--wet-run", "--yes", "--update-ipam", "--site", "Nobody") == 0
    assert len(fake.writes()) == n


def test_cli_missing_csv_is_reported(cli_env, capsys):
    fake, _, tmp = cli_env
    assert run_cli(fake, str(tmp / "nope.csv"), "--dry-run") == 2


# ── menu ─────────────────────────────────────────────────────────────────────────────
def menu_app(cli_env):
    fake, csv_path, tmp = cli_env
    from eipdhcp.config import Settings
    st = Settings(host="h", logs_dir=str(tmp / "logs"), runs_dir=str(tmp / "runs"))
    return fake, App(st, Api(fake, sleep=lambda s: None), csv_path), tmp


def feed(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr(ui, "ask", lambda prompt: next(it))


def test_menu_create_with_preview_and_confirmation(cli_env, monkeypatch, capsys):
    fake, app, _ = menu_app(cli_env)
    feed(monkeypatch, "3", "YES", "0")
    assert run_menu(app) == 0
    out = capsys.readouterr().out
    assert fake.scopes and "DRY" in out and "WET" in out


def test_menu_declining_changes_nothing(cli_env, monkeypatch):
    fake, app, _ = menu_app(cli_env)
    feed(monkeypatch, "3", "no", "0")
    run_menu(app)
    assert not fake.writes()


def test_menu_update_verify_and_last_run(cli_env, monkeypatch, capsys):
    fake, app, _ = menu_app(cli_env)
    feed(monkeypatch, "3", "YES", "5", "4", "l", "0")
    run_menu(app)
    out = capsys.readouterr().out
    assert "verified" in out and "Nothing to change" in out and "created" in out


def test_menu_site_selection_parts_and_batch(cli_env, monkeypatch):
    fake, app, _ = menu_app(cli_env)
    feed(monkeypatch, "7", "1", "8", "3,4", "9", "5", "k", "0")
    run_menu(app)
    assert app.site_filter == ["Site A"] and app.parts == frozenset({"ipam", "dhcp"})
    assert app.batch == 5 and app.keep_names is True


def test_menu_bad_csv_does_not_crash(cli_env, monkeypatch, capsys):
    fake, app, tmp = menu_app(cli_env)
    app.csv_path = str(tmp / "missing.csv")
    feed(monkeypatch, "2", "0")
    assert run_menu(app) == 0
    assert "not found" in capsys.readouterr().out


def test_menu_batch_pause_still_works_after_the_menu_confirmation(cli_env, monkeypatch, capsys):
    fake, app, tmp = menu_app(cli_env)
    rows = "".join(f'TestSpace,S{i},10.2.{i}.0/24,smart.dhcp.example,10,200,10.2.{i}.1,"1.1.1.1",fo1.dhcp\n' for i in range(1, 4))
    open(app.csv_path, "w").write(HEADER + rows)
    app.batch = 2
    feed(monkeypatch, "3", "YES", "n", "0")             # create -> confirm -> stop after the first batch -> exit
    run_menu(app)
    assert len([s for s in fake.scopes if s["dhcp_id"] == "2"]) == 2
    out = capsys.readouterr().out
    assert "Stopped after 2 of 3" in out
    after_stop = out.split("Stopped after 2 of 3")[1]          # the wet run summary, not the earlier preview
    assert "Stopped early" in after_stop and "1 site(s) not run" in after_stop and "All done" not in after_stop


def bare_app(tmp_path):
    from eipdhcp.config import Settings
    st = Settings(host="h", logs_dir=str(tmp_path / "logs"), runs_dir=str(tmp_path / "runs"))
    return FakeSDS(), App(st, Api(FakeSDS(), sleep=lambda s: None), "")


def test_menu_without_a_csv_lists_the_csv_files_and_takes_a_number(tmp_path, monkeypatch, capsys):
    (tmp_path / "a_site.csv").write_text(HEADER + WM + "\n")
    (tmp_path / "b_site.csv").write_text(HEADER + WM + "\n")
    (tmp_path / "old_results.csv").write_text("x\n")                         # result files are not offered
    monkeypatch.chdir(tmp_path)
    _, app = bare_app(tmp_path)
    feed(monkeypatch, "2", "0")                                              # pick the 2nd file, then exit
    run_menu(app)
    out = capsys.readouterr().out
    assert app.csv_path.endswith("b_site.csv") and "1 row(s)" in out
    assert "a_site.csv" in out and "old_results" not in out and "CSV problem" not in out


def test_menu_without_a_csv_accepts_a_typed_path(tmp_path, monkeypatch):
    (tmp_path / "x.csv").write_text(HEADER + WM + "\n")
    monkeypatch.chdir(tmp_path)
    _, app = bare_app(tmp_path)
    feed(monkeypatch, str(tmp_path / "x.csv"), "0")
    run_menu(app)
    assert app.csv_path.endswith("x.csv")


def test_menu_actions_say_what_to_do_when_no_csv_is_chosen(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _, app = bare_app(tmp_path)
    feed(monkeypatch, "", "5", "1", "d", "0")                                # Enter = cancel the chooser first
    assert run_menu(app) == 0
    out = capsys.readouterr().out
    assert out.count("Choose a CSV file first (option 6).") == 3
    assert "CSV file not found" not in out and "none chosen yet - press 6" in out


def test_menu_unknown_path_is_reported_and_the_menu_stays_open(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _, app = bare_app(tmp_path)
    feed(monkeypatch, "nope.csv", "0")
    assert run_menu(app) == 0 and "not found: nope.csv" in capsys.readouterr().out and not app.csv_path


def test_menu_number_out_of_range_is_explained(tmp_path, monkeypatch, capsys):
    (tmp_path / "only.csv").write_text(HEADER + WM + "\n")
    monkeypatch.chdir(tmp_path)
    _, app = bare_app(tmp_path)
    feed(monkeypatch, "5", "0")
    run_menu(app)
    assert "there is no file number 5 (choose 1-1)" in capsys.readouterr().out and not app.csv_path
