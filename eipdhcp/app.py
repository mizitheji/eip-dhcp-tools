"""Ties settings, API and CSV together. Used by both the CLI and the menu."""
import os

from . import ui
from . import validate as val
from .engine import ALL_PARTS
from .rows import build_sites, filter_sites, read_csv
from .runner import RunOptions, print_issues, run_sites


class App:
    def __init__(self, settings, api, csv_path, site_filter=None, batch=0,
                 keep_names=False, yes=False, log_path=""):
        self.settings, self.api = settings, api
        self.csv_path = csv_path
        self.site_filter = list(site_filter or [])
        self.batch, self.keep_names, self.yes, self.log_path = batch, keep_names, yes, log_path
        self.parts = ALL_PARTS
        self.validate_first = True
        self.any_name = False
        self.allow_used = False

    def load_all(self):
        if not self.csv_path:
            raise FileNotFoundError("no CSV file chosen")
        if not os.path.exists(self.csv_path):
            raise FileNotFoundError(f"CSV file not found: {self.csv_path}")
        _, rows = read_csv(self.csv_path)
        return build_sites(rows, self.settings)

    def load_sites(self):
        """Sites after the --site filter. Warns about unknown names."""
        sites, unknown = filter_sites(self.load_all(), self.site_filter)
        for n in unknown:
            ui.tprint(f"  {ui.yellow('⚠')}  site '{n}' not found in the CSV - check the spelling")
        return sites

    def validate(self, online=True):
        sites = self.load_sites()
        issues = val.validate(sites, self.api if online else None)
        print_issues(issues, len(sites))
        if not issues:
            ui.tprint(f"  {ui.green('✔')} no problems found")
        return issues

    def run(self, action, dry, parts=None, yes=None, confirmed=False):
        sites = self.load_sites()
        opts = RunOptions(action=action, dry=dry, parts=frozenset(parts or self.parts),
                          keep_names=self.keep_names, yes=self.yes if yes is None else yes, confirmed=confirmed,
                          batch=self.batch, csv_path=self.csv_path, log_path=self.log_path,
                          validate_first=self.validate_first, any_name=self.any_name, allow_used=self.allow_used)
        return run_sites(self.api, self.settings, sites, opts)
