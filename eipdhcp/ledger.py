"""Run ledger: a JSON file listing every object a run created or changed (one file per wet run)."""
import json
import os
from datetime import datetime


class Ledger:
    def __init__(self, runs_dir, action, host, csv_path):
        os.makedirs(runs_dir, exist_ok=True)
        self.stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(runs_dir, f"{self.stamp}_{action}.json")
        self.data = {"run": self.stamp, "action": action, "host": host, "csv": csv_path,
                     "started": datetime.now().isoformat(timespec="seconds"), "sites": []}
        self.save()

    def begin(self, site):
        rec = {"site": site.name, "network": site.key, "csv_line": site.line, "status": "running",
               "created": {}, "updated": [], "steps": []}
        self.data["sites"].append(rec)
        return rec

    @staticmethod
    def created(rec, kind, oid):
        if kind and oid:
            rec["created"].setdefault(kind, []).append(str(oid))

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def finish(self):
        self.data["finished"] = datetime.now().isoformat(timespec="seconds")
        self.save()


def latest(runs_dir):
    """Path of the newest ledger file, or None."""
    try:
        files = sorted(f for f in os.listdir(runs_dir) if f.endswith(".json"))
    except OSError:
        return None
    return os.path.join(runs_dir, files[-1]) if files else None
