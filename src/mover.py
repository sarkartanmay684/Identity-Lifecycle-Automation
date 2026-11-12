"""Reconcile mapped department access for previously provisioned lab accounts."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import time

from .graph_writer import WriteError
from .joiner import JoinerDirectory, DETAIL_FIELDS, attributes_for
from .joiner_plan import PlanError, object_id


CHANGING = {"department", "jobTitle"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class MoverDirectory(JoinerDirectory):
    def user_by_id(self, identifier):
        identifier = object_id({"id": identifier}, "Recorded employee")
        user = self.client.get(f"/users/{identifier}", params={"$select": DETAIL_FIELDS})
        if object_id(user, "Employee") != identifier:
            raise PlanError("Graph returned a different object ID for the recorded employee.")
        return user


@dataclass
class MoverItem:
    row: object
    user_id: str
    attributes: dict
    groups: dict
    desired_ids: set
    manager: dict
    source_attributes: dict
    record: dict
    before: dict | None = None

    @property
    def identity(self):
        return {"attributes": self.attributes, "group_ids": sorted(self.desired_ids),
                "manager_id": self.manager["id"]}

    @property
    def group_ids(self):
        return {name: group["id"] for name, group in self.groups.items()}


def check_user(item, directory, user):
    directory._checked_users.clear()
    identifier = directory.check_lab_user(user, f"Employee {item.row.employee_id}")
    if identifier != item.user_id or user.get("accountEnabled") is not True:
        raise PlanError(f"Employee {item.row.employee_id}: object ID changed or account disabled; mover stopped.")
    for key, expected in item.attributes.items():
        actual = user.get(key)
        if key in CHANGING:
            if actual not in (item.source_attributes[key], expected):
                raise PlanError(f"Employee {item.row.employee_id}: {key} changed outside this move; review before continuing.")
        else:
            if key in {"employeeId", "userPrincipalName"}:
                actual, expected = str(actual).casefold(), str(expected).casefold()
            if actual != expected:
                raise PlanError(f"Employee {item.row.employee_id}: {key} differs from the recorded identity. Renaming/adoption is outside this mover.")


def snapshot(item, directory):
    user = directory.user_by_id(item.user_id)
    check_user(item, directory, user)
    member_of = {group["id"] for group in item.groups.values()
                 if item.user_id in directory.member_ids(group["id"])}
    return {"user": user, "memberships": member_of,
            "manager_correct": item.user_id in directory.report_ids(item.manager["id"])}


def prepare_movers(employees, mapping, directory, today, journal):
    if any(row.status == "terminated" for row in employees):
        raise PlanError("The mover command rejects terminated rows. Use a dedicated leaver workflow.")
    directory.verify_tenant()
    names = set(mapping.common_groups)
    for values in mapping.department_groups.values():
        names.update(values)
    groups = {name: directory.group(name) for name in sorted(names)}
    if len({group["id"] for group in groups.values()}) != len(groups):
        raise PlanError("Group mapping aliases resolve to the same ID; use one consistent name for each group.")
    items, deferred = [], []
    managers = {}
    for row in sorted(employees, key=lambda value: value.employee_id.casefold()):
        if row.start_date > today:
            deferred.append({"target": row.employee_id, "outcome": "deferred", "effective_date": row.start_date.isoformat()})
            continue
        record = deepcopy(journal.get(row.employee_id))
        if not record or record.get("status") not in {"complete", "moving"}:
            raise PlanError(f"Employee {row.employee_id}: a completed joiner record (or pending move) is required. No account will be created or adopted.")
        identity = record.get("identity")
        if not isinstance(identity, dict) or not isinstance(identity.get("attributes"), dict):
            raise PlanError("The recorded identity is incomplete. Preserve the protected journal.")
        identifier = object_id({"id": record.get("user_id")}, "Recorded employee")
        matches = directory.users_by("employeeId", row.employee_id)
        if len(matches) > 1 or (matches and object_id(matches[0], "Employee") != identifier):
            raise PlanError(f"Employee {row.employee_id}: duplicate or changed directory identity; mover stopped.")
        # Use the recorded object ID for authoritative account reads. A collection
        # query returning no match is never permission to recreate an account.
        current = directory.user_by_id(identifier)
        if current.get("department") not in mapping.department_groups:
            raise PlanError(f"Employee {row.employee_id}: current department is outside the configured mapping.")
        upn = identity["attributes"].get("userPrincipalName")
        if not isinstance(upn, str) or "@" not in upn:
            raise PlanError("The recorded username is invalid. Preserve the journal.")
        expected = attributes_for(row, upn)
        for key, value in expected.items():
            if key not in CHANGING and identity["attributes"].get(key) != value:
                raise PlanError(f"Employee {row.employee_id}: feed changes identity fields; only department, title and manager moves are supported.")
        if row.manager_upn not in managers:
            managers[row.manager_upn] = directory.manager(row.manager_upn)
        manager = managers[row.manager_upn]
        if identifier == manager["id"]:
            raise PlanError(f"Employee {row.employee_id}: cannot be their own manager.")
        desired_ids = {groups[name]["id"] for name in mapping.groups_for(row.department)}
        source = {key: current.get(key) for key in CHANGING}
        item = MoverItem(row, identifier, expected, groups, desired_ids, manager, source, record)
        if record["status"] == "moving":
            pending = record.get("mover_pending")
            if (not isinstance(pending, dict) or pending.get("target_identity") != item.identity
                    or pending.get("group_ids") != item.group_ids
                    or not isinstance(pending.get("source_attributes"), dict)
                    or set(pending["source_attributes"]) != CHANGING
                    or not isinstance(pending.get("operations"), list)):
                raise PlanError(f"Employee {row.employee_id}: a pending move has different inputs. Resume its original feed and group mapping.")
            item.source_attributes = pending["source_attributes"]
        item.before = snapshot(item, directory)
        items.append(item)
    return items, deferred


def changes(item, state):
    return {"remove": state["memberships"] - item.desired_ids,
            "add": item.desired_ids - state["memberships"],
            "patch": {key: item.attributes[key] for key in sorted(CHANGING)
                      if state["user"].get(key) != item.attributes[key]},
            "manager": not state["manager_correct"]}


def group_for_id(item, identifier):
    return next(group for group in item.groups.values() if group["id"] == identifier)


def plan_events(items):
    for item in items:
        delta = changes(item, item.before)
        base = {"target": item.row.employee_id, "user_id": item.user_id, "outcome": "planned"}
        if item.before["user"]["department"] != item.row.department:
            yield {**base, "action": "department_change", "from_department": item.before["user"]["department"],
                   "to_department": item.row.department, "csv_status": item.row.status}
        for identifier in sorted(delta["remove"]):
            yield {**base, "action": "remove_group_membership", "group": group_for_id(item, identifier)}
        for identifier in sorted(delta["add"]):
            yield {**base, "action": "add_group_membership", "group": group_for_id(item, identifier)}
        if delta["patch"]:
            yield {**base, "action": "update_attributes", "before": {key: item.before["user"].get(key) for key in delta["patch"]},
                   "after": delta["patch"]}
        if delta["manager"]:
            yield {**base, "action": "set_manager", "manager": item.manager}
        if not any(delta.values()):
            yield {**base, "action": "no_change", "outcome": "unchanged"}


def verify_item(item, directory):
    state = snapshot(item, directory)
    return not any(changes(item, state).values())


def wait_for(check, sleep):
    for delay in (0, 2, 4, 8, 16):
        if delay:
            sleep(delay)
        if check():
            return True
    return False


def operation_matches(item, directory, operation):
    state = snapshot(item, directory)
    action = operation["action"]
    if action == "remove_group_membership":
        return operation["group"]["id"] not in state["memberships"]
    if action == "add_group_membership":
        return operation["group"]["id"] in state["memberships"]
    if action == "update_attributes":
        return all(state["user"].get(key) == value for key, value in operation["after"].items())
    if action == "set_manager":
        return state["manager_correct"]
    raise PlanError("The pending mover operation cannot be classified.")


def perform(item, directory, journal, emit, action, method, path, body, details, sleep):
    record = item.record
    if record.get("status") != "moving" or not isinstance(record.get("mover_pending"), dict):
        raise PlanError(f"Employee {item.row.employee_id}: directory state changed after planning. Run a fresh dry run before applying.")
    operation = {"action": action, "requested_at": utc_now(), **details}
    record["mover_pending"]["operations"].append(operation)
    journal.put(item.row.employee_id, record)  # Durable intent precedes each write.
    emit("mover_action", target=item.row.employee_id, user_id=item.user_id,
         action=action, outcome="requested", **details)
    try:
        _, status = directory.client.write(method, path, body)
    except WriteError as error:
        operation["http_status"] = error.status
        operation["response_outcome"] = "unknown" if error.uncertain else "rejected"
        journal.put(item.row.employee_id, record)
        # Resolve a race only by positive, scoped read-back. No blind replay.
        if ((action == "remove_group_membership" and error.status == 404)
                or (action == "add_group_membership" and error.status == 400)):
            validate_group(item, directory, details["group"])
            if operation_matches(item, directory, operation):
                operation["verified_at"] = utc_now()
                journal.put(item.row.employee_id, record)
                emit("mover_action", target=item.row.employee_id, user_id=item.user_id, action=action,
                     outcome="already_absent" if method == "DELETE" else "already_present",
                     http_status=error.status, **details)
                return False
        raise
    operation["http_status"] = status
    operation["response_outcome"] = "accepted"
    journal.put(item.row.employee_id, record)
    emit("mover_action", target=item.row.employee_id, user_id=item.user_id,
         action=action, outcome="applied", http_status=status, **details)
    if not wait_for(lambda: operation_matches(item, directory, operation), sleep):
        raise PlanError(f"Employee {item.row.employee_id}: {action} verification is not yet visible. Preserve the journal; wait, dry-run, then resume the same mover feed.")
    operation["verified_at"] = utc_now()
    journal.put(item.row.employee_id, record)
    emit("mover_action_verified", target=item.row.employee_id, user_id=item.user_id,
         action=action, outcome="success", **details)
    return True


def validate_group(item, directory, group):
    current = directory.group(group["displayName"])
    if current["id"] != group["id"]:
        raise PlanError("A mapped group was replaced after planning. Stop and review the mapping.")


def apply_movers(items, directory, journal, emit, *, sleep=time.sleep):
    counts = {"changed_users": 0, "unchanged_users": 0, "removed_memberships": 0,
              "added_memberships": 0, "updated_users": 0, "assigned_managers": 0, "verified_users": 0}
    for item in items:
        target, record = item.row.employee_id, item.record
        initial = snapshot(item, directory)
        delta = changes(item, initial)
        if record["status"] == "complete" and any(delta.values()):
            record.setdefault("initial_identity", deepcopy(record["identity"]))
            record["status"] = "moving"
            record["mover_pending"] = {"started_at": utc_now(), "target_identity": item.identity,
                                       "source_attributes": item.source_attributes,
                                       "group_ids": item.group_ids, "operations": []}
            journal.put(target, record)
        if record["status"] == "moving":
            for operation in record["mover_pending"]["operations"]:
                if "verified_at" not in operation and operation_matches(item, directory, operation):
                    operation["verified_at"] = utc_now()
                    journal.put(target, record)
                    emit("mover_recovery", target=target, user_id=item.user_id, action=operation["action"],
                         outcome="desired_state_observed_on_resume", requested_at=operation["requested_at"],
                         **{key: operation[key] for key in ("group", "after", "manager") if key in operation})
        writes_before = directory.client.writes_succeeded
        if initial["user"]["department"] != item.row.department:
            emit("mover_department_change", target=target, user_id=item.user_id, outcome="detected",
                 from_department=initial["user"]["department"], to_department=item.row.department, csv_status=item.row.status)
        for identifier in sorted(delta["remove"]):
            current = snapshot(item, directory)
            if identifier not in current["memberships"]:
                continue
            group = group_for_id(item, identifier)
            validate_group(item, directory, group)
            if perform(item, directory, journal, emit, "remove_group_membership", "DELETE",
                       f"/groups/{identifier}/members/{item.user_id}/$ref", None, {"group": group}, sleep):
                counts["removed_memberships"] += 1
        for identifier in sorted(item.desired_ids):
            current = snapshot(item, directory)
            if current["memberships"] - item.desired_ids:
                raise PlanError(f"Employee {target}: outgoing managed access remains. No new access will be granted.")
            if identifier in current["memberships"]:
                continue
            group = group_for_id(item, identifier)
            validate_group(item, directory, group)
            if perform(item, directory, journal, emit, "add_group_membership", "POST",
                       f"/groups/{identifier}/members/$ref",
                       {"@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{item.user_id}"},
                       {"group": group}, sleep):
                counts["added_memberships"] += 1
        current = snapshot(item, directory)
        patch = changes(item, current)["patch"]
        if patch:
            if perform(item, directory, journal, emit, "update_attributes", "PATCH", f"/users/{item.user_id}", patch,
                       {"before": {key: current["user"].get(key) for key in patch}, "after": patch}, sleep):
                counts["updated_users"] += 1
        current = snapshot(item, directory)
        if not current["manager_correct"]:
            manager = directory.manager(item.row.manager_upn)
            if manager["id"] != item.manager["id"]:
                raise PlanError("The manager account changed after planning.")
            if perform(item, directory, journal, emit, "set_manager", "PUT", f"/users/{item.user_id}/manager/$ref",
                       {"@odata.id": f"https://graph.microsoft.com/v1.0/users/{item.manager['id']}"},
                       {"manager": item.manager}, sleep):
                counts["assigned_managers"] += 1
        if not wait_for(lambda: verify_item(item, directory), sleep):
            raise PlanError(f"Employee {target}: final verification has not converged. Preserve the journal and resume later.")
        if record["status"] == "moving":
            pending = record.pop("mover_pending")
            pending["completed_at"] = utc_now()
            record.setdefault("mover_history", []).append(pending)
            record["identity"] = item.identity
            record["status"] = "complete"
            journal.put(target, record)
        counts["verified_users"] += 1
        counts["changed_users" if directory.client.writes_succeeded > writes_before else "unchanged_users"] += 1
        emit("mover_verified", target=target, user_id=item.user_id, outcome="success",
             user_principal_name=item.attributes["userPrincipalName"], account_enabled=True,
             department=item.row.department, job_title=item.row.job_title, manager_upn=item.row.manager_upn,
             required_groups=sorted(name for name, group in item.groups.items() if group["id"] in item.desired_ids),
             unwanted_managed_memberships=[])
    return counts
