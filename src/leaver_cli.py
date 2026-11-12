"""Preview, execute, verify and safely resume lab termination."""

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
from .leaver import (LeaverDirectory, apply_leavers, load_replacements, plan_events,
                     prepare_leavers, verify_controls, verify_leaver)
from .leaver_writer import LeaverWriter
from .preview_joiners import argument_date


ROOT = Path(__file__).resolve().parents[1]
WRITE_ACTIONS = {"disable_account", "revoke_sessions", "remove_group_membership",
                 "remove_direct_licenses", "reassign_direct_report"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Read current state and print the plan; no writes.")
    mode.add_argument("--apply", action="store_true", help="Disable and clean up effective, owned lab leavers.")
    mode.add_argument("--verify", action="store_true", help="Read directory state and saved revocation evidence.")
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--group-map", type=Path, default=ROOT / "config/group_map.json")
    parser.add_argument("--manager-replacements", type=Path, help="Explicit JSON map: leaver employee ID to approved replacement manager UPN.")
    parser.add_argument("--as-of", type=argument_date, help="UTC scheduling date for dry-run only.")
    parser.add_argument("--log-file", type=Path, help="Required for apply; append structured audit to a .jsonl file.")
    parser.add_argument("--retry-unconfirmed-revocation", action="store_true",
                        help="Apply only: explicitly retry a previously unconfirmed revocation after operator review.")
    args = parser.parse_args(argv)
    if args.as_of and not args.dry_run:
        parser.error("--as-of is supported only with --dry-run.")
    if args.apply and not args.log_file:
        parser.error("--apply requires --log-file PATH.jsonl.")
    if args.log_file and (not args.apply or args.log_file.suffix.casefold() != ".jsonl"):
        parser.error("--log-file requires --apply and a .jsonl destination.")
    if args.retry_unconfirmed_revocation and not args.apply:
        parser.error("--retry-unconfirmed-revocation requires --apply.")
    actor, client = "local-validation", None
    mode_name = "apply" if args.apply else "verify" if args.verify else "dry_run"

    def emit(event, *, audit=True, **values):
        line = json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "event": event,
                           "actor": actor, "target": "leaver_batch", "mode": mode_name,
                           "read_only": not args.apply, **values}, ensure_ascii=True)
        if audit and args.log_file:
            with args.log_file.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        print(line, flush=True)

    try:
        mapping = load_group_map(args.group_map)
        employees = load_feed(args.csv, mapping)
        replacements = load_replacements(args.manager_replacements, employees)
        config = load_config()
        actor = config.client_id
        today = args.as_of or datetime.now(timezone.utc).date()
        if args.log_file:
            args.log_file.parent.mkdir(parents=True, exist_ok=True)
            with args.log_file.open("a", encoding="utf-8"):
                pass
        if args.apply:
            with Journal(config) as journal, LeaverWriter(config) as client:
                directory = LeaverDirectory(client, config)
                items, controls, notices, groups = prepare_leavers(
                    employees, mapping, directory, today, journal, replacements)
                client.set_scope(items)
                counts = apply_leavers(items, controls, groups, directory, journal, emit,
                                       sleep=client._sleep, retry_unconfirmed=args.retry_unconfirmed_revocation)
                for notice in notices:
                    emit("leaver_notice", **notice)
                emit("leaver_apply_summary", outcome="success", eligible_leavers=len(items),
                     **counts, deferred_rows=sum(n["outcome"] == "deferred" for n in notices),
                     skipped_rows=sum(n["outcome"] == "skipped" for n in notices),
                     directory_writes=client.writes_succeeded, write_attempts=client.write_attempts)
            return 0
        journal = Journal(config).load()
        with GraphClient(config) as client:
            directory = LeaverDirectory(client, config)
            items, controls, notices, groups = prepare_leavers(
                employees, mapping, directory, today, journal, replacements)
            for notice in notices:
                emit("leaver_notice", **notice)
            if args.verify:
                verified = 0
                for item in items:
                    ok = verify_leaver(item, directory)
                    verified += int(ok)
                    emit("leaver_verification", target=item.row.employee_id, user_id=item.user_id,
                         outcome="success" if ok else "failure")
                controls_verified = verify_controls(controls, directory, groups, items, emit)
                emit("leaver_verify_summary", outcome="success" if verified == len(items) else "failure",
                     verified_leavers=verified, eligible_leavers=len(items), active_controls_verified=controls_verified,
                     directory_writes=0, notices=len(notices))
                return 0 if verified == len(items) else 1
            events = list(plan_events(items))
            for event in events:
                emit("leaver_plan", **event)
            for control in controls:
                emit("leaver_active_control", target=control["target"], user_id=control["user_id"],
                     outcome="protected", account_enabled=True)
            actions = {name: sum(event["action"] == name for event in events) for name in sorted(WRITE_ACTIONS)}
            emit("leaver_plan_summary", outcome="success", eligible_leavers=len(items),
                 active_controls=len(controls), actions=actions, planned_directory_writes=sum(actions.values()),
                 pending_verifications=sum(event["outcome"] == "pending" for event in events),
                 deferred_rows=sum(n["outcome"] == "deferred" for n in notices),
                 skipped_rows=sum(n["outcome"] == "skipped" for n in notices), directory_writes=0)
        return 0
    except (ConfigError, GraphError, InputError, PlanError, StateError) as error:
        values = {"outcome": "failure", "message": str(error),
                  "writes_confirmed": getattr(client, "writes_succeeded", 0),
                  "write_attempts": getattr(client, "write_attempts", 0)}
        try:
            emit("leaver_error", **values)
        except OSError:
            emit("leaver_error", audit=False, audit_file_failed=True, **values)
        return 1
    except OSError:
        emit("leaver_error", audit=False, outcome="failure",
             message="Local journal or audit-file access failed. Preserve state and check file access.",
             writes_confirmed=getattr(client, "writes_succeeded", 0),
             write_attempts=getattr(client, "write_attempts", 0))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
