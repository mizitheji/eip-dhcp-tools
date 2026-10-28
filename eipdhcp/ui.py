"""Console output, colours and log-file setup.

Console = tidy summary lines only (tprint / step).  Everything else, including
API calls, replies and tracebacks, goes to the log file only.
"""
import logging
import os
import sys
from datetime import datetime

log = logging.getLogger("dhcp_scopes")
WIDTH = 64

_TTY = sys.stdout.isatty()


def _c(code, t):
    return f"\033[{code}m{t}\033[0m" if _TTY else str(t)


def bold(t):    return _c("1", t)
def dim(t):     return _c("2", t)
def green(t):   return _c("92", t)
def yellow(t):  return _c("93", t)
def red(t):     return _c("91", t)
def cyan(t):    return _c("96", t)
def magenta(t): return _c("95", t)


def tprint(msg=""):
    print(msg)


def rule(char="─"):
    return dim(char * WIDTH)


# status -> (symbol, colour function)
STATUS = {
    "ok":   ("✔", green),
    "dry":  ("▷", yellow),
    "skip": ("⊘", dim),
    "fail": ("✘", red),
    "warn": ("⚠", yellow),
    "diff": ("≠", yellow),
    "info": ("•", cyan),
}


def step(area, status, text):
    """One result line of a site, e.g. '  SCOPE  ✔ created (id=12)'."""
    sym, colour = STATUS[status]
    label = f"{area:<6}"
    tprint(f"  {cyan(label)} {colour(sym)} {text}")
    log.info("  %-6s [%s] %s", area, status, text)


def row_header(idx, total, site, tag):
    prog = f"[{idx}/{total}]"
    tprint(f"\n{rule()}")
    tprint(f" {bold(cyan(prog))}  {bold(site.name)}")
    if site.network is not None:
        rng = f"   {dim('range')} {site.start} – {site.end}" if site.start and site.end else ""
        tprint(f"  {dim('net')} {site.network}{rng}")
    tprint(f"  {dim('srv')} {site.dhcp_server}   {dim('fo')} {site.failover or dim('-')}   {dim('mode')} {tag}")
    tprint(rule())
    log.info("")
    log.info("### ROW %d/%d START ### csv_line=%s site=%s net=%s", idx, total, site.line, site.name, site.network)


def ask(prompt):
    """input() wrapper (easy to monkeypatch in tests)."""
    return input(prompt)


def confirm(prompt, expected="YES"):
    return ask(f"{prompt} Type {expected} to continue: ").strip() == expected


def table(headers, rows):
    """Plain aligned table."""
    widths = [len(str(h)) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))
    line = "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    tprint(bold(line))
    for r in rows:
        tprint("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))


def setup_logging(label, logs_dir):
    """Log everything (DEBUG) to a file. Returns the log file path.
    Nothing is attached to the console on purpose: tracebacks must not clutter it."""
    os.makedirs(logs_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(logs_dir, f"dhcp_scopes_{label}_{stamp}.log")
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    log.setLevel(logging.DEBUG)
    log.propagate = True
    return path
