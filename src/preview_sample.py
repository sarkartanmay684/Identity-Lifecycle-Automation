"""Preview a synthetic IT joiner in the configured tenant. This entry point has no apply mode."""

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from . import cli
from .config import ConfigError, load_config
from .hr_feed import COLUMNS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--dry-run", action="store_true", required=True,
                        help="Read the configured directory and print planned changes.")
    parser.parse_args(argv)
    try:
        config = load_config()
        row = {
            "employee_id": "ILAB-PREVIEW-01", "first_name": "Asha", "last_name": "Preview",
            "department": "IT", "job_title": "IT Lab Preview",
            "manager_upn": "it.manager@" + config.upn_domain,
            "start_date": datetime.now(timezone.utc).date().isoformat(), "end_date": "", "status": "active",
        }
        # Use the existing validated joiner path. The temporary file has fictional HR data only.
        # The historical CSV, protected journal and group mapping are never modified here.
        with TemporaryDirectory(prefix="identity-lab-preview-") as folder:
            feed = Path(folder) / "preview.csv"
            with feed.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=COLUMNS)
                writer.writeheader()
                writer.writerow(row)
            return cli.main(["joiner", "--dry-run", "--csv", str(feed)])
    except (ConfigError, OSError) as exc:
        print(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                          "actor": "local", "target": "sample_preview", "event": "sample_error",
                          "outcome": "failure", "read_only": True, "directory_writes": 0,
                          "message": str(exc) if isinstance(exc, ConfigError)
                          else "Could not access the temporary preview file or local project files."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
