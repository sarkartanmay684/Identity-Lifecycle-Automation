"""Explicit mover dry-run, apply and read-only verification commands."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .config import ConfigError, load_config
from .graph_client import GraphClient, GraphError
from .hr_feed import InputError, load_feed, load_group_map
from .joiner_plan import PlanError
from .joiner_state import Journal, StateError
from .mover import MoverDirectory, apply_movers, plan_events, prepare_movers, verify_item
from .mover_writer import MoverWriter
from .preview_joiners import argument_date


ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Print planned changes using read-only Graph requests.")
    mode.add_argument("--apply", action="store_true", help="Apply mapped group, department, job title and manager changes.")
    mode.add_argument("--verify", action="store_true", help="Verify the directory against this feed without writing.")
    parser.add_argument("--csv", type=Path, required=True, help="Explicit desired-state CSV; no implicit baseline feed.")
    parser.add_argument("--group-map", type=Path, default=ROOT / "config/group_map.json")
    parser.add_argument("--as-of", type=argument_date, help="Scheduling date for dry-run only; defaults to UTC today.")
    parser.add_argument("--log-file", type=Path, help="Append UTF-8 JSON audit lines to this .jsonl file; apply only.")
    args = parser.parse_args(argv)
    if args.as_of and not args.dry_run:
        parser.error("--as-of is supported only with --dry-run.")
    if args.log_file and (not args.apply or args.log_file.resolve().suffix.casefold() != ".jsonl"):
        parser.error("--log-file requires --apply and a destination ending in .jsonl.")
    actor, client = "local-validation", None
    mode_name = "apply" if args.apply else "verify" if args.verify else "dry_run"

    def emit(event, *, audit=True, **values):
        line = json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "event": event,
                           "actor": actor, "target": "mover_batch", "mode": mode_name,
                           "read_only": not args.apply, **values}, ensure_ascii=True)
        if audit and args.log_file:
            with args.log_file.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        print(line, flush=True)

    try:
        if args.log_file:
            args.log_file.parent.mkdir(parents=True, exist_ok=True)
            with args.log_file.open("a", encoding="utf-8"):
                pass
        mapping = load_group_map(args.group_map)
        employees = load_feed(args.csv, mapping)
        config = load_config()
        actor = config.client_id
        today = args.as_of or datetime.now(timezone.utc).date()
        if args.apply:
            with Journal(config) as journal:
                with MoverWriter(config) as client:
                    directory = MoverDirectory(client, config)
                    items, deferred = prepare_movers(employees, mapping, directory, today, journal)
                    client.set_scope(items)
                    counts = apply_movers(items, directory, journal, emit, sleep=client._sleep)
                    for item in deferred:
                        emit("mover_deferred", **item)
                    emit("mover_apply_summary", outcome="success", created_users=0, **counts,
                         deferred_users=len(deferred), directory_writes=client.writes_succeeded,
                         write_attempts=client.write_attempts)
            return 0
        journal = Journal(config).load()
        with GraphClient(config) as client:
            directory = MoverDirectory(client, config)
            items, deferred = prepare_movers(employees, mapping, directory, today, journal)
            if args.verify:
                verified = 0
                for item in items:
                    passed = verify_item(item, directory)
                    verified += int(passed)
                    emit("mover_verification", target=item.row.employee_id, user_id=item.user_id,
                         user_principal_name=item.attributes["userPrincipalName"],
                         outcome="success" if passed else "failure", expected_department=item.row.department,
                         expected_job_title=item.row.job_title, expected_manager=item.row.manager_upn)
                emit("mover_verify_summary", outcome="success" if verified == len(items) else "failure",
                     verified_users=verified, eligible_users=len(items), deferred_users=len(deferred), directory_writes=0)
                return 0 if verified == len(items) else 1
            events = list(plan_events(items))
            for event in events:
                emit("mover_plan", **event)
            for item in deferred:
                emit("mover_deferred", **item)
            counts = {action: sum(event["action"] == action for event in events) for action in (
                "department_change", "remove_group_membership", "add_group_membership", "update_attributes", "set_manager", "no_change")}
            emit("mover_plan_summary", outcome="success", created_users=0, actions=counts,
                 planned_directory_writes=sum(counts[key] for key in (
                     "remove_group_membership", "add_group_membership", "update_attributes", "set_manager")),
                 deferred_users=len(deferred), directory_writes=0)
            return 0
    except (ConfigError, GraphError, InputError, PlanError, StateError) as error:
        values = dict(outcome="failure", message=str(error),
                      writes_confirmed=getattr(client, "writes_succeeded", 0), write_attempts=getattr(client, "write_attempts", 0))
        try:
            emit("mover_error", **values)
        except OSError:
            emit("mover_error", audit=False, audit_file_failed=True, **values)
        return 1
    except OSError:
        emit("mover_error", audit=False, outcome="failure", message="Local journal or audit-file access failed. Preserve state and check file access before resuming.",
             writes_confirmed=getattr(client, "writes_succeeded", 0), write_attempts=getattr(client, "write_attempts", 0))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
