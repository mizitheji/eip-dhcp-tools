"""Interactive numbered menu on top of App (the command-line flags keep working for automation)."""
import json
import os

from . import ui
from .engine import ALL_PARTS
from .ledger import latest
from .rows import filter_sites

PART_LABELS = [("ipam", "IPAM block + network"), ("dhcp", "DHCP scope + range"),
               ("pool", "IPAM pool over the range"), ("options", "router / DNS options")]


def _header(app):
    n, names = 0, []
    if app.csv_path:
        try:
            sites = app.load_all()
            n, names = len(sites), [s.name for s in sites]
        except Exception as e:                     # missing or unreadable CSV
            ui.tprint(ui.red(f"  CSV problem: {e}"))
    flt = ", ".join(app.site_filter) if app.site_filter else "all"
    inc = " ".join(f"{'[x]' if p in app.parts else '[ ]'}{p}" for p, _ in PART_LABELS)
    ui.tprint(f"\n{ui.bold('─' * ui.WIDTH)}")
    ui.tprint(f"  {ui.bold('EfficientIP DHCP tool')}   {ui.dim(app.settings.host)}")
    if app.csv_path:
        ui.tprint(f"  {ui.dim('csv')}     {app.csv_path}   {ui.dim(f'{n} row(s)')}")
    else:
        ui.tprint(f"  {ui.dim('csv')}     {ui.yellow('none chosen yet - press 6')}")
    ui.tprint(f"  {ui.dim('sites')}   {flt}      {ui.dim('batch')} {app.batch or 'off'}      {ui.dim('keep names')} {'yes' if app.keep_names else 'no'}")
    ui.tprint(f"  {ui.dim('include')} {inc}")
    ui.tprint(ui.bold("─" * ui.WIDTH))
    return names


def _preview_then_apply(app, action):
    """Dry run first, then ask before anything is changed."""
    summary = app.run(action, dry=True)
    if summary.count("planned") == 0:
        ui.tprint(ui.green("  Nothing to change."))
        return
    if ui.confirm(f"\n{summary.count('planned')} site(s) have changes. Apply them to the server?"):
        app.run(action, dry=False, confirmed=True)
    else:
        ui.tprint("  Not applied.")


def find_csvs(app):
    """CSV files in the current folder and in the tool folder (result files excluded)."""
    folders = []
    for d in (os.getcwd(), os.path.dirname(os.path.abspath(app.settings.runs_dir))):
        if d not in folders and os.path.isdir(d):
            folders.append(d)
    found = []
    for d in folders:
        for name in sorted(os.listdir(d)):
            if name.lower().endswith(".csv") and not name.endswith("_results.csv"):
                path = os.path.join(d, name)
                shown = os.path.relpath(path) if os.path.abspath(d) == os.getcwd() else path
                if shown not in found:
                    found.append(shown)
    return found


def _choose_csv(app):
    found = find_csvs(app)
    if found:
        ui.tprint("  CSV files found:")
        for i, f in enumerate(found, 1):
            ui.tprint(f"   {i:>3}) {f}")
    raw = ui.ask("  Number, or type a path (Enter = cancel): ").strip().strip("'\"")
    if not raw:
        return
    if raw.isdigit() and not 1 <= int(raw) <= len(found):
        ui.tprint(ui.red(f"  there is no file number {raw}" + (f" (choose 1-{len(found)})" if found else "; type a path instead")))
        return
    path = found[int(raw) - 1] if raw.isdigit() else os.path.expanduser(raw)
    if os.path.isfile(path):
        app.csv_path = path
        ui.tprint(f"  using {path}")
    else:
        ui.tprint(ui.red(f"  not found: {path}"))


def _choose_sites(app, names):
    if not names:
        ui.tprint(ui.red("  load a CSV first"))
        return
    for i, n in enumerate(names, 1):
        ui.tprint(f"   {i:>3}) {n}")
    raw = ui.ask("  Numbers separated by commas, or 'all': ").strip().lower()
    if raw in ("", "all"):
        app.site_filter = []
        return
    picked = []
    for tok in raw.replace(" ", "").split(","):
        if tok.isdigit() and 1 <= int(tok) <= len(names):
            picked.append(names[int(tok) - 1])
    app.site_filter = picked
    ui.tprint(f"  selected: {', '.join(picked) or 'none (check the numbers)'}")


def _choose_parts(app):
    for i, (p, label) in enumerate(PART_LABELS, 1):
        ui.tprint(f"   {i}) [{'x' if p in app.parts else ' '}] {label}")
    raw = ui.ask("  Numbers to switch on/off (e.g. 3,4), Enter to keep: ").strip()
    parts = set(app.parts)
    for tok in raw.replace(" ", "").split(","):
        if tok.isdigit() and 1 <= int(tok) <= len(PART_LABELS):
            p = PART_LABELS[int(tok) - 1][0]
            parts.symmetric_difference_update({p})
    if "pool" in parts and "ipam" not in parts:
        parts.discard("pool")
        ui.tprint(ui.yellow("  the pool needs IPAM; pool switched off"))
    app.parts = frozenset(parts) if parts else ALL_PARTS


def _last_run(app):
    path = latest(app.settings.runs_dir)
    if not path:
        ui.tprint("  no wet run recorded yet")
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    ui.tprint(f"  {ui.bold(os.path.basename(path))}   {data.get('action')}  {data.get('started')}")
    for rec in data.get("sites", []):
        made = ", ".join(f"{k}:{'/'.join(v)}" for k, v in rec.get("created", {}).items()) or "-"
        ui.tprint(f"   • {rec['site']}  {rec['network']}  [{rec['status']}]  created {made}  updated {len(rec.get('updated', []))}  deleted {len(rec.get('deleted', []))}")
    ui.tprint(f"  {ui.dim('logs folder:')} {app.settings.logs_dir}")


NEEDS_CSV = {"1", "2", "3", "4", "5", "7", "d"}


def _in_use_refusals(summary):
    """Sites refused only because IPAM shows addresses in use."""
    return [site for site, o in summary.results if o.status == "refused" and "in use in IPAM" in (o.detail or "")]


def _delete_flow(app):
    summary = app.run("delete", dry=True)
    blocked = _in_use_refusals(summary)
    if summary.count("planned"):
        app.run("delete", dry=False)              # asks for the typed DELETE confirmation itself
    elif not blocked:
        ui.tprint("  Nothing can be deleted (see the notes above).")
    if not blocked or app.allow_used:
        return
    ui.tprint(f"\n  {ui.yellow('⚠')}  {len(blocked)} site(s) were refused because IPAM shows addresses in use "
              f"(listed above):")
    for site in blocked:
        ui.tprint(f"    • {site.name}   {site.key}")
    ui.tprint("  Deleting them removes those IPAM address records too. Check the list before you agree.")
    if ui.ask("  Type YES to delete these anyway (Enter = skip): ").strip() != "YES":
        ui.tprint("  Skipped.")
        return
    saved_filter = app.site_filter
    app.allow_used, app.site_filter = True, [site.name for site in blocked]
    try:
        if app.run("delete", dry=True).count("planned"):
            app.run("delete", dry=False)          # still asks for the typed DELETE <n> phrase
        else:
            ui.tprint("  Nothing can be deleted (see the notes above).")
    finally:
        app.allow_used, app.site_filter = False, saved_filter


def run_menu(app):
    if not app.csv_path:
        _choose_csv(app)
    while True:
        names = _header(app)
        ui.tprint("   1) Validate the CSV")
        ui.tprint("   2) Dry run - preview creating NEW sites")
        ui.tprint("   3) Create new sites (preview, then confirm)")
        ui.tprint("   4) Update existing sites to match the CSV (preview, then confirm)")
        ui.tprint("   5) Verify sites (read everything back and compare)")
        ui.tprint(f"   6) Choose CSV file          7) Choose sites        8) Choose what to include")
        ui.tprint(f"   9) Batch size               k) Keep names (update)  l) Show last wet run")
        ui.tprint(f"   d) DELETE sites (IPAM network + block; the DHCP scope/range follow)")
        ui.tprint("   0) Exit")
        choice = ui.ask("\n  Choose: ").strip().lower()
        if choice in NEEDS_CSV and not app.csv_path:
            ui.tprint(ui.yellow("  Choose a CSV file first (option 6)."))
            continue
        try:
            if choice == "1":
                app.validate()
            elif choice == "2":
                app.run("create", dry=True)
            elif choice == "3":
                _preview_then_apply(app, "create")
            elif choice == "4":
                _preview_then_apply(app, "update")
            elif choice == "5":
                app.run("verify", dry=True)
            elif choice == "6":
                _choose_csv(app)
            elif choice == "7":
                _choose_sites(app, names)
            elif choice == "8":
                _choose_parts(app)
            elif choice == "9":
                raw = ui.ask("  Ask to continue after every N sites (0 = never): ").strip()
                app.batch = int(raw) if raw.isdigit() else app.batch
            elif choice == "k":
                app.keep_names = not app.keep_names
            elif choice == "l":
                _last_run(app)
            elif choice == "d":
                _delete_flow(app)
            elif choice in ("0", "q", "exit", ""):
                if choice == "":
                    continue
                return 0
            else:
                ui.tprint(ui.yellow("  unknown choice"))
        except FileNotFoundError as e:
            ui.tprint(ui.red(f"  {e}"))
        except KeyboardInterrupt:
            ui.tprint(ui.yellow("\n  interrupted - back to the menu"))
        except Exception as e:
            ui.log.exception("menu action failed")
            ui.tprint(ui.red(f"  error: {e}  (details in the log file)"))
