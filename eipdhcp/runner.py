"""Run one action (create | update | verify) over a list of sites."""
import csv
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime

from . import ui
from . import validate as val
from .engine import ALL_PARTS, Ctx, SiteOutcome, execute, plan_site, verify
from .ledger import Ledger

log = logging.getLogger("dhcp_scopes")

BAD = {"failed", "partial", "mismatch", "invalid", "refused"}


@dataclass
class RunOptions:
    action: str = "create"            # create | update | verify
    dry: bool = True
    parts: frozenset = ALL_PARTS
    keep_names: bool = False
    any_name: bool = False
    allow_used: bool = False
    yes: bool = False                 # skip ALL prompts (confirmation and batch pauses)
    confirmed: bool = False           # already confirmed (menu): skip only the YES prompt
    batch: int = 0
    validate_first: bool = True
    csv_path: str = ""
    log_path: str = ""


@dataclass
class Summary:
    results: list = field(default_factory=list)       # (site, SiteOutcome)
    ledger_path: str = ""
    results_csv: str = ""
    aborted: bool = False

    def count(self, *statuses):
        return sum(1 for _, o in self.results if o.status in statuses)

    @property
    def failed(self):
        return self.count(*BAD)

    @property
    def exit_code(self):
        return 1 if self.failed else 0


def print_issues(issues, n_sites):
    c = val.counts(issues)
    ui.tprint(f"\n{ui.bold('VALIDATION')}  {n_sites} row(s)   "
              f"{(ui.red if c[val.ERROR] else ui.green)(str(c[val.ERROR]) + ' error(s)')}   "
              f"{(ui.yellow if c[val.WARN] else ui.dim)(str(c[val.WARN]) + ' warning(s)')}   "
              f"{ui.dim(str(c[val.INFO]) + ' note(s)')}")
    sym = {val.ERROR: ui.red("✘"), val.WARN: ui.yellow("⚠"), val.INFO: ui.cyan("•")}
    for i in issues:
        ui.tprint(f"  {sym[i.level]} line {i.line:<3} {ui.bold(i.site or '?')}: {i.msg}")
        log.info("validation %s line=%s site=%s: %s", i.level, i.line, i.site, i.msg)


def _status_line(out):
    s = out.status
    if s in ("applied", "verified"):
        return ui.green(ui.bold("✔ done")) + ui.dim(f" ({out.detail})")
    if s == "deleted":
        return ui.green(ui.bold("✔ deleted")) + ui.dim(f" ({out.detail})")
    if s == "in-sync":
        return ui.green(ui.bold("✔ in sync")) + ui.dim(" (nothing to do)")
    if s == "planned":
        return ui.yellow(ui.bold(f"≠ {out.changes} change(s) planned"))
    if s == "skipped":
        return ui.yellow(ui.bold("⊘ skipped"))
    if s == "refused":
        return ui.red(ui.bold("⊘ refused")) + ui.dim(" (nothing was deleted for this site)")
    if s == "partial":
        return ui.yellow(ui.bold("⚠ partly done")) + ui.dim(f" ({out.detail})")
    return ui.red(ui.bold(f"✘ {s}")) + ui.dim(f" ({out.detail})")


def _write_results(runs_dir, opts, results):
    os.makedirs(runs_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(runs_dir, f"{stamp}_{opts.action}_{'dry' if opts.dry else 'wet'}_results.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["csv_line", "site", "network", "status", "detail"])
        for site, out in results:
            w.writerow([site.line, site.name, site.key, out.status, out.detail])
    return path


def run_sites(api, settings, sites, opts):
    summary = Summary()
    action, dry = opts.action, opts.dry
    changing = action in ("create", "update", "delete")
    tag = (ui.yellow("▷ DRY") if dry else ui.green("▶ WET")) if changing else ui.cyan("◎ VERIFY")
    tag = f"{tag} {action}" if changing else tag

    to_run = list(sites)
    if changing and opts.validate_first:
        issues = val.validate_delete(sites, api) if action == "delete" else val.validate(sites, api, action=action)
        print_issues(issues, len(sites))
        bad = val.error_lines(issues)
        for s in sites:
            if s.line in bad:
                first = next(i.msg for i in issues if i.line == s.line and i.level == val.ERROR)
                summary.results.append((s, SiteOutcome("invalid", first, failures=1)))
        to_run = [s for s in sites if s.line not in bad]

    mode_note = ("read-only: nothing will be changed" if (dry or not changing)
                 else ("objects WILL be DELETED on the server" if action == "delete" else "changes WILL be applied to the server"))
    ui.tprint(f"\n{ui.bold('─' * ui.WIDTH)}")
    ui.tprint(f"  {ui.bold(action.upper())} {'DRY RUN' if dry and changing else ('WET RUN' if changing else '')}  "
              f"{ui.cyan(str(len(to_run)) + ' row(s)')}  {ui.dim('→  ' + settings.host)}")
    ui.tprint(f"  {ui.dim('csv')}   {opts.csv_path}")
    if opts.log_path:
        ui.tprint(f"  {ui.dim('log')}   {opts.log_path}")
    ui.tprint(f"  {ui.dim('note')}  {(ui.red if (changing and not dry) else ui.dim)(mode_note)}")
    ui.tprint(ui.bold("─" * ui.WIDTH))
    log.info("%s %s: %d row(s) -> %s", action, "dry" if dry else "wet", len(to_run), settings.host)

    if not to_run:
        ui.tprint("  nothing to run")
    elif action == "delete" and not dry:
        # always a typed phrase; --yes does not skip it
        ui.tprint(f"\n  {ui.red(ui.bold('DELETE'))} will remove these from {settings.host}:")
        for s_ in to_run:
            ui.tprint(f"    • {s_.name}   {s_.key}")
        ui.tprint("  The IPAM network (and its block when it holds only that network) is deleted;")
        ui.tprint("  the DHCP scope, range and pool follow. A copy of every object is saved first.")
        phrase = f"DELETE {len(to_run)}"
        if ui.ask(f"\n  Type {phrase} to continue: ").strip() != phrase:
            ui.tprint("Aborted.")
            summary.aborted = True
            return summary
    elif changing and not dry and not (opts.yes or opts.confirmed):
        if not ui.confirm(f"\nApply to {settings.host}?"):
            ui.tprint("Aborted.")
            summary.aborted = True
            return summary

    ledger = Ledger(settings.runs_dir, action, settings.host, opts.csv_path) if (changing and not dry and to_run) else None
    if ledger:
        summary.ledger_path = ledger.path

    total = len(to_run)
    for idx, site in enumerate(to_run, start=1):
        ui.row_header(idx, total, site, tag)
        rec = ledger.begin(site) if ledger else None
        ctx = Ctx(api, settings, site, parts=opts.parts, keep_names=opts.keep_names,
                  any_name=opts.any_name, allow_used=opts.allow_used,
                  mode={"update": "update", "verify": "update", "delete": "delete"}.get(action, "create"))
        try:
            if action == "verify":
                out = verify(ctx)
            else:
                out = execute(ctx, plan_site(ctx), dry, rec, ledger)
        except Exception as e:
            log.exception("row %s failed", site.line)
            ui.step("ERROR", "fail", str(e))
            out = SiteOutcome("failed", str(e), failures=1)
        if rec is not None:
            rec["status"], rec["detail"] = out.status, out.detail
            ledger.save()
        ui.tprint(f"  {_status_line(out)}")
        summary.results.append((site, out))

        if (opts.batch and changing and not dry and not opts.yes and idx % opts.batch == 0 and idx < total):
            if ui.ask(f"\n{idx} of {total} done. Continue with the next {opts.batch}? [y/N] ").strip().lower() != "y":
                summary.aborted = True
                ui.tprint(ui.yellow(f"Stopped after {idx} of {total} row(s)."))
                break

    if ledger:
        ledger.finish()

    # ── summary ──
    ui.tprint(f"\n{ui.bold('─' * ui.WIDTH)}")
    by = {}
    for _, o in summary.results:
        by[o.status] = by.get(o.status, 0) + 1
    parts_txt = "  ".join(f"{n} {st}" for st, n in sorted(by.items()))
    not_run = total - sum(1 for site, _ in summary.results if site in to_run)
    if summary.aborted and not_run > 0:
        ui.tprint(f"  {ui.yellow(ui.bold('⚠ Stopped early'))}  {parts_txt}  {ui.yellow(str(not_run) + ' site(s) not run')}")
        if summary.failed:
            for site, o in summary.results:
                if o.status in BAD:
                    ui.tprint(f"    {ui.red('•')} line {site.line}  {ui.bold(site.name)}: [{o.status}] {o.detail}")
    elif summary.failed:
        ui.tprint(f"  {ui.red(ui.bold('✘ Completed with problems'))}  {parts_txt}")
        for site, o in summary.results:
            if o.status in BAD:
                ui.tprint(f"    {ui.red('•')} line {site.line}  {ui.bold(site.name)}: [{o.status}] {o.detail}")
    else:
        ui.tprint(f"  {ui.green(ui.bold('✔ All done'))}  {parts_txt or 'nothing to do'}")
    if dry and changing:
        ui.tprint(f"  {ui.dim('Nothing was changed. Re-run with --wet-run to apply.')}")
    if summary.results:
        summary.results_csv = _write_results(settings.runs_dir, opts, summary.results)
        ui.tprint(f"  {ui.dim('Results:')}  {summary.results_csv}")
    if summary.ledger_path:
        ui.tprint(f"  {ui.dim('Ledger: ')}  {summary.ledger_path}")
    if opts.log_path:
        ui.tprint(f"  {ui.dim('Full log:')} {opts.log_path}")
    ui.tprint(ui.bold("─" * ui.WIDTH))
    return summary
