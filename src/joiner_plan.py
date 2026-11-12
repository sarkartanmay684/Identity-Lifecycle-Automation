"""Build a new-user-only plan using GET requests. This module cannot apply it."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from .hr_feed import Employee, GroupMap, InputError, username_base


USER_FIELDS = (
    "id,userPrincipalName,employeeId,displayName,department,jobTitle,"
    "accountEnabled,userType,onPremisesSyncEnabled,onPremisesImmutableId"
)
GROUP_FIELDS = "id,displayName,securityEnabled,mailEnabled,groupTypes,isAssignableToRole,onPremisesSyncEnabled"


class PlanError(ValueError):
    """Directory data cannot safely support a joiner preview."""


def odata_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def object_id(item: dict, label: str) -> str:
    try:
        return str(UUID(item["id"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise PlanError(f"{label}: Graph did not return a valid object ID.") from None


class DirectoryReader:
    def __init__(self, client, config):
        self.client = client
        self.config = config
        self._checked_users = set()
        self._upn_cache = {}

    def verify_tenant(self):
        organizations = self.client.get_all("/organization", params={"$select": "id,displayName,verifiedDomains"})
        if len(organizations) != 1 or organizations[0].get("id") != self.config.tenant_id:
            raise PlanError("The directory tenant does not match ENTRA_TENANT_ID.")
        domains = organizations[0].get("verifiedDomains")
        if not isinstance(domains, list):
            raise PlanError("Graph did not return verified domains.")
        if self.config.upn_domain not in {str(item.get("name", "")).lower() for item in domains if isinstance(item, dict)}:
            raise PlanError("ENTRA_UPN_DOMAIN is not a verified domain in this tenant.")
        return organizations[0].get("displayName")

    def users_by(self, property_name: str, value: str):
        if property_name not in {"employeeId", "userPrincipalName"}:
            raise ValueError("Unsupported identity lookup.")
        users = self.client.get_all("/users", params={
            "$filter": f"{property_name} eq {odata_text(value)}", "$select": USER_FIELDS,
        })
        if any(str(user.get(property_name, "")).casefold() != value.casefold() for user in users):
            raise PlanError("Graph returned a user that did not match the requested identifier.")
        return users

    def check_lab_user(self, user: dict, label: str):
        identifier = object_id(user, label)
        required = {"userType", "userPrincipalName", "onPremisesSyncEnabled", "onPremisesImmutableId", "accountEnabled"}
        if not required.issubset(user):
            raise PlanError(f"{label}: cannot verify all required account properties.")
        if user["userType"] != "Member":
            raise PlanError(f"{label}: only Member users are within the lab scope.")
        if user["onPremisesSyncEnabled"] not in (None, False) or user["onPremisesImmutableId"]:
            raise PlanError(f"{label}: synchronized or immutable-ID-linked accounts are outside the lab scope.")
        if not isinstance(user["accountEnabled"], bool):
            raise PlanError(f"{label}: accountEnabled must be a Boolean.")
        if not str(user["userPrincipalName"]).lower().endswith("@" + self.config.upn_domain):
            raise PlanError(f"{label}: user principal name is outside the configured lab domain.")
        if identifier not in self._checked_users:
            # Plain collection query avoids advanced-query headers and also returns
            # directoryRole type/ID when only limited information is available.
            memberships = self.client.get_all(f"/users/{identifier}/transitiveMemberOf")
            for membership in memberships:
                kind = membership.get("@odata.type")
                if kind == "#microsoft.graph.directoryRole":
                    raise PlanError(f"{label}: an active directory-role membership was found; account excluded.")
                if kind not in {"#microsoft.graph.group", "#microsoft.graph.administrativeUnit"}:
                    raise PlanError(f"{label}: cannot classify a returned directory membership.")
            self._checked_users.add(identifier)
        return identifier

    def manager(self, upn: str):
        matches = self.users_by("userPrincipalName", upn)
        if len(matches) != 1:
            raise PlanError(f"Manager {upn}: expected exactly one existing user, found {len(matches)}.")
        manager = matches[0]
        identifier = self.check_lab_user(manager, f"Manager {upn}")
        if manager["accountEnabled"] is not True:
            raise PlanError(f"Manager {upn}: the lab manager account must be enabled.")
        return {"id": identifier, "userPrincipalName": manager["userPrincipalName"]}

    def group(self, name: str):
        matches = self.client.get_all("/groups", params={
            "$filter": f"displayName eq {odata_text(name)}", "$select": GROUP_FIELDS,
        })
        if len(matches) != 1:
            raise PlanError(f"Group {name}: expected exactly one match, found {len(matches)}. Use unique group names.")
        group = matches[0]
        if str(group.get("displayName", "")).casefold() != name.casefold():
            raise PlanError(f"Group {name}: Graph returned a different display name.")
        if not set(GROUP_FIELDS.split(",")).issubset(group):
            raise PlanError(f"Group {name}: cannot verify all required group properties.")
        if (
            group["securityEnabled"] is not True or group["mailEnabled"] is not False
            or group["groupTypes"] != [] or group["isAssignableToRole"] not in (None, False)
            or group["onPremisesSyncEnabled"] not in (None, False)
        ):
            raise PlanError(f"Group {name}: use a cloud-only Assigned security group without role assignment or mail enablement.")
        return {"id": object_id(group, f"Group {name}"), "displayName": group["displayName"]}

    def upn_exists(self, upn: str):
        if upn.casefold() not in self._upn_cache:
            self._upn_cache[upn.casefold()] = bool(self.users_by("userPrincipalName", upn))
        return self._upn_cache[upn.casefold()]


def reserve_upn(employee: Employee, domain: str, directory: DirectoryReader, reserved: set[str]):
    try:
        base = username_base(employee.first_name, employee.last_name)
    except ValueError as error:
        raise InputError(f"Employee {employee.employee_id}: {error}.") from None
    for number in range(1, 1001):
        suffix = "" if number == 1 else str(number)
        local = base[:64 - len(suffix)].rstrip(".") + suffix
        upn = local + "@" + domain
        if len(upn) > 113:
            raise PlanError("Generated UPN exceeds the lab's 113-character limit; review the configured domain and naming rule.")
        if upn.casefold() not in reserved and not directory.upn_exists(upn):
            reserved.add(upn.casefold())
            return upn
    raise PlanError(f"Employee {employee.employee_id}: no free username among 1,000 deterministic candidates.")


def build_joiner_plan(employees: list[Employee], mapping: GroupMap, directory: DirectoryReader, today: date):
    """Return a complete plan, or raise. Never emit a partial valid-looking plan."""
    directory.verify_tenant()
    managers = {upn: directory.manager(upn) for upn in sorted({row.manager_upn for row in employees})}
    group_names = {name for row in employees for name in mapping.groups_for(row.department)}
    groups = {name: directory.group(name) for name in sorted(group_names)}
    records, reserved = [], set()
    # Stable ordering also makes two same-name joiners deterministic within one feed.
    for row in sorted(employees, key=lambda employee: employee.employee_id.casefold()):
        matches = directory.users_by("employeeId", row.employee_id)
        if len(matches) > 1:
            raise PlanError(f"Employee {row.employee_id}: multiple directory users have this employeeId.")
        existing = matches[0] if matches else None
        if existing:
            directory.check_lab_user(existing, f"Employee {row.employee_id}")
            if existing["id"] == managers[row.manager_upn]["id"]:
                raise PlanError(f"Employee {row.employee_id}: a user cannot be their own manager.")

        def note(outcome, reason, **details):
            records.append({"action": "skip", "target": row.employee_id, "outcome": outcome, "reason": reason, **details})

        if row.status == "terminated":
            if row.end_date > today:
                note("deferred", "future_termination", effective_date=row.end_date.isoformat())
            elif existing is None:
                note("skipped", "terminated_employee_not_found")
            else:
                note("pending", "leaver_not_implemented", user_id=existing["id"])
            continue
        if existing:
            note(
                "pending", "existing_account_requires_reconciliation", user_id=existing["id"],
                account_enabled=existing["accountEnabled"],
                current_department=existing.get("department"), requested_department=row.department,
                department_changed=existing.get("department") != row.department,
            )
            continue
        if row.start_date > today:
            note("deferred", "future_start", effective_date=row.start_date.isoformat())
            continue
        upn = reserve_upn(row, directory.config.upn_domain, directory, reserved)
        records.append({
            "action": "create_user", "target": row.employee_id, "outcome": "planned",
            "attributes": {
                "employeeId": row.employee_id, "userPrincipalName": upn,
                "displayName": row.first_name + " " + row.last_name,
                "givenName": row.first_name, "surname": row.last_name,
                "mailNickname": upn.split("@", 1)[0], "department": row.department,
                "jobTitle": row.job_title, "accountEnabled": True, "userType": "Member",
            },
            "password_policy": {"forceChangePasswordNextSignIn": True, "generation": "apply_phase_only"},
        })
        for name in mapping.groups_for(row.department):
            records.append({
                "action": "add_group_membership", "target": row.employee_id,
                "outcome": "planned", "group": groups[name], "after": "create_user",
            })
        records.append({
            "action": "set_manager", "target": row.employee_id, "outcome": "planned",
            "manager": managers[row.manager_upn], "after": "create_user",
        })
    return records
