"""Identity lifecycle commands: joiner, mover, leaver and local credential storage."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from .config import ConfigError, load_config
from .graph_client import GraphClient, GraphError
from .graph_writer import GraphWriter
from .hr_feed import InputError, load_feed, load_group_map
from .joiner import JoinerDirectory, apply, plan_events, prepare, verify_item
from .joiner_plan import PlanError
from .joiner_state import Journal, StateError, WindowsProtector
from .preview_joiners import argument_date


ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "leaver":
        from .leaver_cli import main as leaver_main
        return leaver_main(argv[1:])
    if argv and argv[0] == "mover":
        from .mover_cli import main as mover_main
        return mover_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("mover", add_help=False, help="Preview, apply or verify department moves; run mover --help.")
    commands.add_parser("leaver", add_help=False, help="Preview, apply or verify terminations; run leaver --help.")
    joiner = commands.add_parser("joiner", help="Plan, create, or verify lab joiners.")
    mode = joiner.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Read the directory and journal; make no changes.")
    mode.add_argument("--apply", action="store_true", help="Create accounts and reconcile tracked joiner access in Entra.")
    mode.add_argument("--verify", action="store_true", help="Read and verify the required account, group, and manager state.")
    joiner.add_argument("--csv", type=Path, default=ROOT / "data/hr_feed.csv")
    joiner.add_argument("--group-map", type=Path, default=ROOT / "config/group_map.json")
    joiner.add_argument("--as-of", type=argument_date, help="UTC scheduling date, accepted only with --dry-run.")
    commands.add_parser("storage-check", help="Test Windows password protection locally; no Graph calls.")
    password = commands.add_parser("password", help="Privately display a stored initial password in an interactive terminal.")
    password.add_argument("--employee-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "joiner" and args.as_of and not args.dry_run:
        parser.error("--as-of is supported only with --dry-run; apply always uses today's UTC date.")
    actor = "local"
    mode_name = "apply" if getattr(args, "apply", False) else "verify" if getattr(args, "verify", False) else "dry_run"
    client = None

    def emit(event, **values):
        print(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "event": event,
                          "actor": actor, "target": "joiner_batch", "mode": mode_name,
                          "read_only": mode_name != "apply", **values}, ensure_ascii=True), flush=True)

    try:
        if args.command == "storage-check":
            WindowsProtector().self_check()
            emit("storage_check", outcome="success", graph_requests=0, directory_writes=0)
            return 0
        config = load_config()
        actor = config.client_id
        if args.command == "password":
            # Explicit opt-in command; never available through redirects or JSON logs.
            if not sys.stdout.isatty() or not sys.stdin.isatty():
                raise StateError("Use a normal interactive PowerShell terminal for password retrieval, without output redirection.")
            record = Journal(config).load().get(args.employee_id)
            if not record or record.get("status") != "complete" or not isinstance(record.get("password"), str):
                raise StateError("No completed joiner password is available for that employee.")
            print("Initial password (may have changed after first sign-in):")
            print(record["password"])
            return 0
        mapping = load_group_map(args.group_map)
        employees = load_feed(args.csv, mapping)
        today = args.as_of or datetime.now(timezone.utc).date()
        if args.apply:
            with Journal(config) as journal:
                with GraphWriter(config) as client:
                    directory = JoinerDirectory(client, config)
                    items, deferred = prepare(employees, mapping, directory, today, journal)
                    counts = apply(items, directory, journal, emit)
                    for item in deferred:
                        emit("joiner_deferred", **item)
                    emit("joiner_apply_summary", outcome="success", **counts, deferred_users=len(deferred),
                         directory_writes=client.writes_succeeded, write_attempts=client.write_attempts)
            return 0
        with GraphClient(config) as client:
            directory = JoinerDirectory(client, config)
            journal = None if args.verify else Journal(config).load()
            items, deferred = prepare(employees, mapping, directory, today, journal, require_owned=not args.verify)
            if args.verify:
                verified = 0
                for item in items:
                    passed = item.user_id is not None and verify_item(item, directory)
                    verified += int(passed)
                    emit("joiner_verification", target=item.row.employee_id, outcome="success" if passed else "failure",
                         user_id=item.user_id, user_principal_name=item.attributes["userPrincipalName"],
                         required_groups=[group["displayName"] for group in item.groups], manager_upn=item.row.manager_upn)
                emit("joiner_verify_summary", outcome="success" if verified == len(items) else "failure",
                     verified_users=verified, eligible_users=len(items), deferred_users=len(deferred), directory_writes=0)
                return 0 if verified == len(items) else 1
            events = list(plan_events(items))
            for event in events:
                emit("joiner_plan", **event)
            for item in deferred:
                emit("joiner_deferred", **item)
            counts = {name: sum(row["action"] == name for row in events)
                      for name in ("create_user", "add_group_membership", "set_manager", "no_change")}
            emit("joiner_plan_summary", outcome="success", actions=counts, deferred_users=len(deferred), directory_writes=0)
            return 0
    except (ConfigError, GraphError, InputError, PlanError, StateError) as error:
        emit("joiner_error", outcome="failure", message=str(error),
             writes_confirmed=getattr(client, "writes_succeeded", 0), write_attempts=getattr(client, "write_attempts", 0))
        return 1
    except OSError:
        emit("joiner_error", outcome="failure", message="Local file access failed. Preserve the state folder and inspect file permissions.",
             writes_confirmed=getattr(client, "writes_succeeded", 0), write_attempts=getattr(client, "write_attempts", 0))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
