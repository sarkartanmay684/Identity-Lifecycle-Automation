"""Create lab joiners and reconcile their initial access on safe repeat runs."""

from __future__ import annotations

from dataclasses import dataclass
import secrets
import string
import time

from .graph_writer import WriteError
from .joiner_plan import DirectoryReader, PlanError, USER_FIELDS, object_id, odata_text, reserve_upn


DETAIL_FIELDS = USER_FIELDS + ",givenName,surname,mailNickname"


def new_password():
    classes = (string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#%*-_+")
    characters = [secrets.choice(group) for group in classes]
    characters.extend(secrets.choice("".join(classes)) for _ in range(28))
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


class JoinerDirectory(DirectoryReader):
    def users_by(self, property_name, value):
        if property_name not in {"employeeId", "userPrincipalName"}:
            raise ValueError("Unsupported lookup.")
        result = self.client.get_all("/users", params={
            "$filter": f"{property_name} eq {odata_text(value)}", "$select": DETAIL_FIELDS,
        })
        if any(str(user.get(property_name, "")).casefold() != value.casefold() for user in result):
            raise PlanError("Graph returned a user that did not match the requested identifier.")
        return result

    def find_employee(self, employee_id):
        matches = self.users_by("employeeId", employee_id)
        if len(matches) > 1:
            raise PlanError(f"Employee {employee_id}: more than one directory user has this employeeId.")
        return matches[0] if matches else None

    def member_ids(self, group_id):
        # The existing GroupMember.ReadWrite.All permission supports this read.
        return {object_id(value, "Group member") for value in
                self.client.get_all(f"/groups/{group_id}/members", params={"$select": "id"})}

    def report_ids(self, manager_id):
        # directReports supports application authentication with User.Read.All.
        return {object_id(value, "Manager direct report") for value in
                self.client.get_all(f"/users/{manager_id}/directReports", params={"$select": "id"})}


@dataclass
class JoinerItem:
    row: object
    attributes: dict
    groups: list[dict]
    manager: dict
    user_id: str | None
    missing_groups: list[dict]
    needs_manager: bool

    @property
    def identity(self):
        return {"attributes": self.attributes, "group_ids": sorted(group["id"] for group in self.groups),
                "manager_id": self.manager["id"]}


def attributes_for(row, upn):
    return {
        "employeeId": row.employee_id, "userPrincipalName": upn,
        "displayName": row.first_name + " " + row.last_name,
        "givenName": row.first_name, "surname": row.last_name,
        "mailNickname": upn.split("@", 1)[0], "department": row.department,
        "jobTitle": row.job_title, "accountEnabled": True, "userType": "Member",
    }


def check_account(directory, row, user, expected, expected_id=None):
    # Re-evaluate active roles before changes rather than trusting a cached check.
    directory._checked_users.clear()
    identifier = directory.check_lab_user(user, f"Employee {row.employee_id}")
    if expected_id is not None and identifier != expected_id:
        raise PlanError(f"Employee {row.employee_id}: directory object ID changed; no automatic adoption.")
    if user.get("accountEnabled") is not True:
        raise PlanError(f"Employee {row.employee_id}: account is disabled; this joiner command never re-enables users.")
    for key, value in expected.items():
        actual = user.get(key)
        if key in {"userPrincipalName", "employeeId"}:
            actual, value = str(actual).casefold(), str(value).casefold()
        if actual != value:
            raise PlanError(f"Employee {row.employee_id}: {key} differs from the feed/journal. Review it in the mover stage.")
    return identifier


def prepare(employees, mapping, directory, today, journal=None, *, require_owned=True):
    """Validate the entire batch before allowing any writes."""
    directory.verify_tenant()
    if any(row.status == "terminated" for row in employees):
        raise PlanError("The joiner command does not process terminated rows. Leaver automation is a separate checkpoint.")
    managers = {upn: directory.manager(upn) for upn in sorted({row.manager_upn for row in employees})}
    names = {name for row in employees for name in mapping.groups_for(row.department)}
    groups = {name: directory.group(name) for name in sorted(names)}
    # Exercise these verification reads before creating any account.
    members = {group["id"]: directory.member_ids(group["id"]) for group in groups.values()}
    reports = {manager["id"]: directory.report_ids(manager["id"]) for manager in managers.values()}
    items, deferred, reserved = [], [], set()
    for row in sorted(employees, key=lambda value: value.employee_id.casefold()):
        if row.start_date > today:
            deferred.append({"target": row.employee_id, "outcome": "deferred", "effective_date": row.start_date.isoformat()})
            continue
        existing = directory.find_employee(row.employee_id)
        record = journal.get(row.employee_id) if journal is not None else None
        if require_owned and record and record.get("status") not in {"created", "complete"}:
            raise PlanError(f"Employee {row.employee_id}: a previous creation has an unknown outcome. Preserve the journal and inspect the account; do not repeat POST /users.")
        if existing:
            upn = str(existing.get("userPrincipalName", ""))
        else:
            if record:
                raise PlanError(f"Employee {row.employee_id}: the recorded account is missing. Do not automatically recreate it.")
            upn = reserve_upn(row, directory.config.upn_domain, directory, reserved)
        expected = attributes_for(row, upn)
        identifier = None
        selected_groups = [groups[name] for name in mapping.groups_for(row.department)]
        manager = managers[row.manager_upn]
        if existing:
            identifier = check_account(directory, row, existing, expected)
            if identifier == manager["id"]:
                raise PlanError(f"Employee {row.employee_id}: cannot be their own manager.")
        item = JoinerItem(row, expected, selected_groups, manager, identifier,
                          [group for group in selected_groups if identifier not in members[group["id"]]],
                          identifier not in reports[manager["id"]])
        if existing and require_owned:
            if not record or record.get("user_id") != identifier or record.get("identity") != item.identity:
                raise PlanError(f"Employee {row.employee_id}: not owned by this matching joiner journal, or approved inputs changed. Review before applying.")
        items.append(item)
    return items, deferred


def plan_events(items):
    for item in items:
        target = item.row.employee_id
        if item.user_id is None:
            yield {"action": "create_user", "target": target, "outcome": "planned", "attributes": item.attributes,
                   "forceChangePasswordNextSignIn": True}
        for group in item.missing_groups:
            yield {"action": "add_group_membership", "target": target, "outcome": "planned", "group": group}
        if item.needs_manager:
            yield {"action": "set_manager", "target": target, "outcome": "planned", "manager": item.manager}
        if item.user_id and not item.missing_groups and not item.needs_manager:
            yield {"action": "no_change", "target": target, "outcome": "unchanged", "user_id": item.user_id}


def poll(check, sleep):
    for attempt in range(4):
        result = check()
        if result:
            return result
        if attempt < 3:
            sleep(2 ** attempt)
    return None


def fresh_account(item, directory):
    user = directory.find_employee(item.row.employee_id)
    if user is None:
        return None
    check_account(directory, item.row, user, item.attributes, item.user_id)
    return user


def verify_item(item, directory):
    user = fresh_account(item, directory)
    if user is None:
        return False
    return all(item.user_id in directory.member_ids(group["id"]) for group in item.groups) and item.user_id in directory.report_ids(item.manager["id"])


def apply(items, directory, journal, emit, *, sleep=time.sleep, password_factory=new_password):
    """A failed batch stops; completed operations remain tracked for a safe rerun."""
    counts = {"created_users": 0, "added_memberships": 0, "assigned_managers": 0, "unchanged_users": 0, "verified_users": 0}
    for item in items:
        target = item.row.employee_id
        changed = False
        if item.user_id is None:
            if directory.find_employee(target) is not None or directory.users_by("userPrincipalName", item.attributes["userPrincipalName"]):
                raise PlanError(f"Employee {target}: identity changed after planning. Run a fresh dry run.")
            password = password_factory()
            record = {"status": "creating", "user_id": None, "identity": item.identity, "password": password}
            journal.put(target, record)  # Persist encrypted intent BEFORE sending POST.
            payload = {**item.attributes, "passwordProfile": {"password": password, "forceChangePasswordNextSignIn": True}}
            emit("joiner_action", target=target, action="create_user", outcome="requested")
            try:
                result, status = directory.client.write("POST", "/users", payload)
                item.user_id = object_id(result, "Created user")
            except WriteError as error:
                if not error.uncertain:
                    journal.forget_rejected_creation(target)
                raise
            finally:
                payload["passwordProfile"]["password"] = None
                password = None
            record["user_id"] = item.user_id
            record["status"] = "created"
            journal.put(target, record)
            counts["created_users"] += 1
            changed = True
            emit("joiner_action", target=target, action="create_user", outcome="applied", http_status=status, user_id=item.user_id)
        if not poll(lambda: fresh_account(item, directory), sleep):
            raise PlanError(f"Employee {target}: account verification is not yet visible. Wait, then rerun the same apply command.")
        for group in item.groups:
            # Read current membership every time, including after partial failures.
            if item.user_id in directory.member_ids(group["id"]):
                continue
            if fresh_account(item, directory) is None:
                raise PlanError(f"Employee {target}: account disappeared before the group change.")
            current_group = directory.group(group["displayName"])
            if current_group["id"] != group["id"]:
                raise PlanError("A group changed after planning; run a fresh dry run.")
            emit("joiner_action", target=target, action="add_group_membership", outcome="requested", group=group)
            try:
                _, status = directory.client.write("POST", f"/groups/{group['id']}/members/$ref", {
                    "@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{item.user_id}",
                })
            except WriteError as error:
                if error.status == 400 and item.user_id in directory.member_ids(group["id"]):
                    emit("joiner_action", target=target, action="add_group_membership", outcome="already_present", group=group)
                    continue
                raise
            counts["added_memberships"] += 1
            changed = True
            emit("joiner_action", target=target, action="add_group_membership", outcome="applied", http_status=status, group=group)
            if not poll(lambda: item.user_id in directory.member_ids(group["id"]), sleep):
                raise PlanError(f"Employee {target}: membership verification is not yet visible. Wait, then rerun the same apply command.")
        if item.user_id not in directory.report_ids(item.manager["id"]):
            if fresh_account(item, directory) is None:
                raise PlanError(f"Employee {target}: account disappeared before the manager change.")
            manager = directory.manager(item.row.manager_upn)
            if manager["id"] != item.manager["id"]:
                raise PlanError("A manager changed after planning; run a fresh dry run.")
            emit("joiner_action", target=target, action="set_manager", outcome="requested", manager=item.manager)
            _, status = directory.client.write("PUT", f"/users/{item.user_id}/manager/$ref", {
                "@odata.id": f"https://graph.microsoft.com/v1.0/users/{item.manager['id']}",
            })
            counts["assigned_managers"] += 1
            changed = True
            emit("joiner_action", target=target, action="set_manager", outcome="applied", http_status=status, manager=item.manager)
        if not poll(lambda: verify_item(item, directory), sleep):
            raise PlanError(f"Employee {target}: final access verification did not pass. Wait, then rerun the same apply command.")
        record = journal.get(target)
        record["status"] = "complete"
        journal.put(target, record)
        counts["verified_users"] += 1
        if not changed:
            counts["unchanged_users"] += 1
        emit("joiner_verified", target=target, outcome="success", user_id=item.user_id,
             user_principal_name=item.attributes["userPrincipalName"], account_enabled=True,
             department=item.row.department, job_title=item.row.job_title,
             required_groups=[group["displayName"] for group in item.groups], manager_upn=item.row.manager_upn)
    return counts
