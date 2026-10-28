"""Command line entry point."""
import argparse
import os
import sys

from . import __version__, ui
from .app import App
from .config import env_permission_warning, load_settings
from .engine import ALL_PARTS

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXAMPLES = """
examples:
  python create_dhcp_scopes.py                                   open the interactive menu
  python create_dhcp_scopes.py scopes.csv --validate             check the CSV (and the server)
  python create_dhcp_scopes.py scopes.csv --dry-run              preview creating new sites
  python create_dhcp_scopes.py scopes.csv --wet-run --batch 10   create, asking after every 10 sites
  python create_dhcp_scopes.py scopes.csv --update --dry-run     show what differs on existing sites
  python create_dhcp_scopes.py scopes.csv --update --wet-run     fix those differences
  python create_dhcp_scopes.py scopes.csv --verify               read everything back and compare
  python create_dhcp_scopes.py scopes.csv --verify --site "Site name 1"
  python create_dhcp_scopes.py scopes.csv --delete --dry-run --site "Site name 1"   preview a delete
  python create_dhcp_scopes.py scopes.csv --delete --wet-run --site "Site name 1"   delete (typed confirmation)
"""


def build_parser():
    ap = argparse.ArgumentParser(prog="create_dhcp_scopes.py", epilog=EXAMPLES,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description="Create, update and verify DHCP + IPAM sites on SOLIDserver from a CSV.")
    ap.add_argument("csv_file", nargs="?", help="CSV with one row per network")
    act = ap.add_argument_group("action (default: create)")
    g = act.add_mutually_exclusive_group()
    g.add_argument("--create", action="store_true", help="create NEW sites; sites whose scope exists are skipped untouched")
    g.add_argument("--update", action="store_true", help="make EXISTING sites match the CSV (creates what is missing, fixes what differs)")
    g.add_argument("--verify", action="store_true", help="read everything back and compare with the CSV (read-only)")
    g.add_argument("--delete", action="store_true", help="DELETE sites: the IPAM network (and its block); the DHCP scope/range/pool follow. Needs a typed confirmation")
    g.add_argument("--validate", action="store_true", help="check the CSV and the server for problems (read-only)")
    mode = ap.add_argument_group("mode (for create / update)")
    m = mode.add_mutually_exclusive_group()
    m.add_argument("--dry-run", action="store_true", help="show what WOULD change; nothing is changed")
    m.add_argument("--wet-run", action="store_true", help="apply the changes (asks for confirmation)")
    ap.add_argument("--menu", action="store_true", help="interactive menu")
    ap.add_argument("--site", action="append", metavar="SITE", help="only this site name (repeatable)")
    ap.add_argument("--yes", action="store_true", help="no confirmation prompts")
    ap.add_argument("--batch", type=int, default=0, metavar="N", help="in a wet run, stop and ask after every N sites")
    ap.add_argument("--offline", action="store_true", help="with --validate: do not contact the server")
    ap.add_argument("--keep-names", action="store_true", help="update: never rename existing networks/scopes")
    ap.add_argument("--any-name", action="store_true", help="delete: do not require the IPAM network name to equal the CSV 'site'")
    ap.add_argument("--allow-used", action="store_true", help="delete: do not refuse when IPAM reports IP addresses in use")
    ap.add_argument("--no-validate", action="store_true", help="skip the automatic validation before create/update")
    sel = ap.add_argument_group("limit what is handled")
    sel.add_argument("--no-ipam", action="store_true", help="DHCP only (no IPAM block/network/pool)")
    sel.add_argument("--skip-pool", action="store_true", help="no IPAM pool over the DHCP range")
    sel.add_argument("--skip-options", action="store_true", help="no router / DNS options")
    sel.add_argument("--options-only", action="store_true", help="only router / DNS options of existing scopes")
    sel.add_argument("--update-ipam", action="store_true", help="shortcut for: --update on the IPAM network only")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap


def parts_from(args):
    parts = set(ALL_PARTS)
    if args.no_ipam:
        parts -= {"ipam", "pool"}
    if args.skip_pool:
        parts.discard("pool")
    if args.skip_options:
        parts.discard("options")
    if args.options_only:
        parts = {"options"}
    if args.update_ipam:
        parts = {"ipam"}
    return frozenset(parts)


def make_api(settings):
    from SOLIDserverRest import SOLIDserverRest
    from .api import Api
    sds = SOLIDserverRest(settings.host)
    sds.set_ssl_verify(settings.verify_ssl)
    sds.use_basicauth_sds(settings.user, settings.password)
    return Api(sds)


def main(argv=None, api_factory=make_api):
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.update_ipam:
        args.update = True
    action = ("validate" if args.validate else "verify" if args.verify else "delete" if args.delete
              else "update" if args.update else "create")
    explicit_action = any((args.validate, args.verify, args.update, args.create, args.delete))
    changing = action in ("create", "update", "delete")

    use_menu = args.menu or (not args.csv_file and not explicit_action and not args.dry_run
                             and not args.wet_run and sys.stdin.isatty())
    if not use_menu:
        if not args.csv_file:
            ap.error("a CSV file is required (or run without arguments for the menu)")
        if changing and not (args.dry_run or args.wet_run):
            ap.error(f"--{action} needs --dry-run or --wet-run")

    offline_only = action == "validate" and args.offline and not use_menu
    settings = load_settings(BASE_DIR, need_credentials=not offline_only)
    warn = env_permission_warning(os.path.join(BASE_DIR, ".env"))
    if warn:
        ui.tprint(f"  {ui.yellow('⚠')}  {warn}")

    label = "menu" if use_menu else (f"{action}_{'dry' if args.dry_run or not changing else 'wet'}")
    log_path = ui.setup_logging(label, settings.logs_dir)

    api = None if offline_only else api_factory(settings)
    csv_path = args.csv_file or ""
    if csv_path and not os.path.isabs(csv_path) and not os.path.exists(csv_path):
        alt = os.path.join(BASE_DIR, csv_path)
        csv_path = alt if os.path.exists(alt) else csv_path
    app = App(settings, api, csv_path, site_filter=args.site, batch=args.batch,
              keep_names=args.keep_names, yes=args.yes, log_path=log_path)
    app.parts = parts_from(args)
    app.validate_first = not args.no_validate
    app.any_name, app.allow_used = args.any_name, args.allow_used

    if use_menu:
        from .menu import run_menu
        return run_menu(app)

    try:
        if action == "validate":
            from . import validate as val
            issues = app.validate(online=not args.offline)
            return 1 if val.counts(issues)[val.ERROR] else 0
        summary = app.run(action, dry=bool(args.dry_run) if changing else True)
        return summary.exit_code
    except FileNotFoundError as e:
        ui.tprint(ui.red(str(e)))
        return 2
