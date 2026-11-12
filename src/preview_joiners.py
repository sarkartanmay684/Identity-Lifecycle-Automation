"""Run: python -m src.preview_joiners --validate-only OR --dry-run."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .config import ConfigError, load_config
from .graph_client import GraphClient, GraphError
from .hr_feed import InputError, iso_date, load_feed, load_group_map
from .joiner_plan import DirectoryReader, PlanError, build_joiner_plan


ROOT = Path(__file__).resolve().parents[1]


def argument_date(value):
    try:
        return iso_date(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a real date in YYYY-MM-DD format.") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate the HR feed and preview new-user actions without changing Entra.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate-only", action="store_true", help="Validate local CSV/JSON only; no authentication or network calls.")
    mode.add_argument("--dry-run", action="store_true", help="Read Graph and print proposed joiner actions; no directory writes.")
    parser.add_argument("--csv", type=Path, default=ROOT / "data" / "hr_feed.csv")
    parser.add_argument("--group-map", type=Path, default=ROOT / "config" / "group_map.json")
    parser.add_argument("--as-of", type=argument_date, help="Preview scheduling at this UTC date; defaults to today's UTC date.")
    args = parser.parse_args(argv)
    today = args.as_of or datetime.now(timezone.utc).date()
    actor = "local-validation"

    def emit(event, **values):
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(), "event": event,
            "actor": actor, "target": str(args.csv), "read_only": True,
            "dry_run": args.dry_run, **values,
        }
        print(json.dumps(record, ensure_ascii=True))

    try:
        mapping = load_group_map(args.group_map)
        employees = load_feed(args.csv, mapping)
        if args.validate_only:
            emit("hr_validation", outcome="success", employees=len(employees),
                 employee_ids=[row.employee_id for row in employees],
                 departments=sorted({row.department for row in employees}),
                 graph_requests=0, as_of_utc=today.isoformat())
            return 0
        config = load_config()
        actor = config.client_id
        with GraphClient(config) as client:
            plan = build_joiner_plan(employees, mapping, DirectoryReader(client, config), today)
        # Only print intended actions after ALL local/directory validation has passed.
        for action in plan:
            emit("joiner_preview", **action)
        counts = {name: sum(row["action"] == name for row in plan)
                  for name in ("create_user", "add_group_membership", "set_manager", "skip")}
        emit("joiner_preview_summary", outcome="success", employees=len(employees),
             as_of_utc=today.isoformat(), actions=counts,
             pending_reconciliation=sum(row["outcome"] == "pending" for row in plan),
             directory_writes=0, scope="new_users_only")
        return 0
    except (InputError, PlanError, ConfigError, GraphError) as error:
        emit("joiner_preview_error", outcome="failure", message=str(error), directory_writes=0)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
