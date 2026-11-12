"""Recoverable termination of journal-owned, cloud-only lab employees."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import time

from .graph_writer import WriteError
from .joiner import JoinerDirectory, DETAIL_FIELDS, attributes_for
from .joiner_plan import PlanError, object_id
from .mover import wait_for


LEAVER_FIELDS = DETAIL_FIELDS + ",assignedLicenses,licenseAssignmentStates,signInSessionsValidFromDateTime"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def stamp(value):
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise PlanError("Graph returned an invalid signInSessionsValidFromDateTime.") from None


def load_replacements(path, employees):
    if path is None:
        return {}
    try:
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key.casefold() in result:
                    raise ValueError()
                result[key.casefold()] = value
            return result
        data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_pairs)
    except (ValueError, OSError, AttributeError):
        raise PlanError("Manager replacement file must be a JSON object with unique employee IDs.") from None
    allowed = {row.employee_id.casefold() for row in employees if row.status == "terminated"}
    if (not isinstance(data, dict) or not set(data) <= allowed
            or not all(isinstance(v, str) and v.strip() == v and "@" in v for v in data.values())):
        raise PlanError("Replacement entries must map terminated employee IDs from this feed to manager UPNs.")
    return {key: value.lower() for key, value in data.items()}


class LeaverDirectory(JoinerDirectory):
    def user_by_id(self, identifier):
        identifier = object_id({"id": identifier}, "Recorded employee")
        user = self.client.get(f"/users/{identifier}", params={"$select": LEAVER_FIELDS})
        if object_id(user, "Employee") != identifier:
            raise PlanError("Graph returned a different user object ID.")
        return user

    def memberships(self, identifier, groups):
        # Reuse the role/membership read from the earlier checkpoints. Include
        # ancestors as a conservative scope check; this lab uses flat groups.
        result = set()
        for value in self.client.get_all(f"/users/{identifier}/transitiveMemberOf"):
            kind = value.get("@odata.type")
            if kind == "#microsoft.graph.group":
                result.add(object_id(value, "Membership"))
            elif kind != "#microsoft.graph.administrativeUnit":
                raise PlanError("An administrator role or unclassified membership is outside the leaver scope.")
        # Independent direct-member reads protect against collection visibility lag.
        result.update(group["id"] for group in groups.values()
                      if identifier in self.member_ids(group["id"]))
        return result


def licenses(user, approved_groups):
    assigned = user.get("assignedLicenses")
    states = user.get("licenseAssignmentStates")
    if not isinstance(assigned, list) or not isinstance(states, list):
        raise PlanError("Licence assignment data is missing. No assumptions about an unlicensed account are allowed.")
    skus = {object_id({"id": value.get("skuId")}, "Licence SKU")
            for value in assigned if isinstance(value, dict)}
    if len(skus) != len(assigned):
        raise PlanError("Licence assignment data is malformed or duplicated.")
    direct, inherited, described = set(), {}, set()
    for value in states:
        if not isinstance(value, dict) or "assignedByGroup" not in value:
            raise PlanError("Licence assignment source cannot be determined.")
        sku = object_id({"id": value.get("skuId")}, "Licence SKU")
        described.add(sku)
        if value["assignedByGroup"] is None:
            direct.add(sku)
        else:
            group_id = object_id({"id": value["assignedByGroup"]}, "Licence group")
            if group_id not in approved_groups:
                raise PlanError("An inherited licence comes from a group outside the configured lab scope.")
            inherited.setdefault(group_id, set()).add(sku)
    if skus - described:
        raise PlanError("Some assigned licences have no source information. Wait for directory consistency and retry.")
    return {"assigned": skus, "direct": direct, "inherited": inherited, "states_empty": not states}


def check_identity(directory, identifier, attributes, user, *, allow_disabled):
    directory._checked_users.clear()
    if directory.check_lab_user(user, "Journal-owned employee") != identifier:
        raise PlanError("The employee object ID changed.")
    for key, expected in attributes.items():
        if key == "accountEnabled" and allow_disabled:
            continue
        actual = user.get(key)
        if key in {"employeeId", "userPrincipalName"}:
            actual, expected = str(actual).casefold(), str(expected).casefold()
        if actual != expected:
            raise PlanError(f"Employee {attributes.get('employeeId')}: {key} differs from the recorded identity; review the feed and journal.")
    if "signInSessionsValidFromDateTime" not in user:
        raise PlanError("Session-revocation timestamp was not returned by Graph.")
    stamp(user["signInSessionsValidFromDateTime"])


@dataclass
class LeaverItem:
    row: object
    user_id: str
    attributes: dict
    groups: dict
    record: dict
    replacement: dict | None
    reports: dict
    before: dict | None = None

    @property
    def group_ids(self):
        return {name: value["id"] for name, value in self.groups.items()}

    @property
    def intent(self):
        return {"end_date": self.row.end_date.isoformat(), "group_ids": self.group_ids,
                "replacement": self.replacement, "attributes": self.attributes}


def checked_user(item, directory):
    user = directory.user_by_id(item.user_id)
    check_identity(directory, item.user_id, item.attributes, user, allow_disabled=True)
    return user


def snapshot(item, directory):
    user = checked_user(item, directory)
    memberships = directory.memberships(item.user_id, item.groups)
    if memberships - set(item.group_ids.values()):
        raise PlanError(f"Employee {item.row.employee_id}: group access outside the configured lab scope requires review.")
    reports = directory.report_ids(item.user_id)
    if reports - set(item.reports):
        raise PlanError("New or unapproved direct reports were found. Review the replacement manager plan.")
    return {"user": user, "memberships": memberships, "reports": reports,
            "licenses": licenses(user, set(item.group_ids.values()))}


def report_state(report, item, directory):
    user = directory.user_by_id(report["user_id"])
    check_identity(directory, report["user_id"], report["attributes"], user, allow_disabled=False)
    old = report["user_id"] in directory.report_ids(item.user_id)
    new = report["user_id"] in directory.report_ids(item.replacement["id"])
    if not old and not new:
        raise PlanError("A direct report's manager changed outside this approved reassignment.")
    return old, new


def control_snapshot(control, directory, groups):
    user = directory.user_by_id(control["user_id"])
    check_identity(directory, control["user_id"], control["attributes"], user, allow_disabled=False)
    return {"memberships": sorted(directory.memberships(control["user_id"], groups)),
            "manager_correct": control["user_id"] in directory.report_ids(control["manager_id"]),
            "assignedLicenses": user.get("assignedLicenses"),
            "licenseAssignmentStates": user.get("licenseAssignmentStates")}


def prepare_leavers(employees, mapping, directory, today, journal, replacements):
    directory.verify_tenant()
    names = set(mapping.common_groups)
    for values in mapping.department_groups.values():
        names.update(values)
    groups = {name: directory.group(name) for name in sorted(names)}
    if len({g["id"] for g in groups.values()}) != len(groups):
        raise PlanError("The group mapping contains aliases to the same group.")
    effective = {row.employee_id.casefold() for row in employees
                 if row.status == "terminated" and row.end_date <= today}
    items, controls, notices = [], [], []
    by_user = {value.get("user_id"): (key, value) for key, value in journal.data["users"].items()
               if isinstance(value, dict) and value.get("user_id")}
    for row in sorted(employees, key=lambda value: value.employee_id.casefold()):
        if row.status == "terminated" and row.end_date > today:
            notices.append({"target": row.employee_id, "outcome": "deferred", "reason": "future_termination",
                            "effective_date": row.end_date.isoformat()})
            continue
        if row.status != "terminated" and row.start_date > today:
            notices.append({"target": row.employee_id, "outcome": "skipped", "reason": "future_start"})
            continue
        matches = directory.users_by("employeeId", row.employee_id)
        if len(matches) > 1:
            raise PlanError(f"Employee {row.employee_id}: duplicate employeeId in the directory.")
        record = deepcopy(journal.get(row.employee_id))
        if not record:
            if matches:
                raise PlanError(f"Employee {row.employee_id}: existing account is not owned by this journal.")
            notices.append({"target": row.employee_id, "outcome": "skipped", "reason": "account_not_found"})
            continue
        allowed = {"complete", "leaving", "left"} if row.status == "terminated" else {"complete"}
        if record.get("status") not in allowed:
            raise PlanError(f"Employee {row.employee_id}: journal state requires review before this workflow.")
        identifier = object_id({"id": record.get("user_id")}, "Recorded employee")
        if matches and object_id(matches[0], "Employee") != identifier:
            raise PlanError("The employeeId now identifies a different user object.")
        identity = record.get("identity", {})
        attributes = identity.get("attributes")
        if not isinstance(attributes, dict) or not attributes.get("userPrincipalName"):
            raise PlanError("The journal identity is incomplete.")
        if attributes_for(row, attributes["userPrincipalName"]) != attributes:
            raise PlanError(f"Employee {row.employee_id}: feed identity does not match the latest completed journal state.")
        user = directory.user_by_id(identifier)
        check_identity(directory, identifier, attributes, user, allow_disabled=row.status == "terminated")
        if row.status != "terminated":
            control = {"target": row.employee_id, "user_id": identifier,
                       "attributes": attributes, "row": row, "record": record}
            controls.append(control)
            continue
        manager = directory.manager(row.manager_upn)
        if manager["id"] != identity.get("manager_id"):
            raise PlanError("The feed manager differs from the recorded manager.")
        if identifier not in directory.report_ids(manager["id"]):
            raise PlanError("The employee's own manager relationship differs from the journal.")
        replacement_upn = replacements.get(row.employee_id.casefold())
        replacement = directory.manager(replacement_upn) if replacement_upn else None
        if replacement and replacement["id"] in {
                value.get("user_id") for key, value in journal.data["users"].items()
                if key in effective and isinstance(value, dict)}:
            raise PlanError("An effective leaver cannot be a replacement manager.")
        current_reports = directory.report_ids(identifier)
        saved = record.get("leaver")
        if record["status"] in {"leaving", "left"} and not isinstance(saved, dict):
            raise PlanError("Leaver recovery state is missing. Preserve the journal.")
        if saved and (not isinstance(saved.get("operations"), list)
                      or not all(isinstance(op, dict) for op in saved["operations"])
                      or not isinstance(saved.get("reports"), dict)
                      or (saved.get("revocation") is not None and not isinstance(saved["revocation"], dict))):
            raise PlanError("Leaver recovery state is malformed. Preserve the journal.")
        reports = deepcopy(saved.get("reports", {})) if saved else {}
        if saved and current_reports - set(reports):
            raise PlanError("Additional direct reports appeared after the approved plan.")
        if current_reports and not replacement:
            raise PlanError(f"Employee {row.employee_id} has direct reports. Supply an approved --manager-replacements JSON file.")
        if not saved:
            for report_id in current_reports:
                pair = by_user.get(report_id)
                if not pair or pair[1].get("status") != "complete" or pair[0] in effective:
                    raise PlanError("Direct reports must be active journal-owned lab users; no out-of-scope reassignment.")
                report_key, report_record = pair
                if report_record["identity"].get("manager_id") != identifier:
                    raise PlanError("A direct report's recorded manager differs from the directory.")
                reports[report_id] = {"employee_key": report_key, "user_id": report_id,
                                      "attributes": deepcopy(report_record["identity"]["attributes"])}
        item = LeaverItem(row, identifier, attributes, groups, record, replacement, reports)
        if saved and saved.get("intent") != item.intent:
            raise PlanError("Termination inputs changed. Resume the original date, mapping and approved manager replacements.")
        if reports and not replacement:
            raise PlanError("The original manager replacement file is required for recovery and verification.")
        for report in reports.values():
            report_record = journal.get(report["employee_key"])
            if (not report_record or report_record.get("status") != "complete"
                    or report_record.get("user_id") != report["user_id"]
                    or report_record.get("identity", {}).get("attributes") != report["attributes"]
                    or report_record["identity"].get("manager_id") not in {identifier, replacement["id"]}):
                raise PlanError("A direct report's journal record changed; review before reassignment.")
            report_state(report, item, directory)
        item.before = snapshot(item, directory)
        items.append(item)
    # A manager can only have one direct parent; fail if journal plans overlap.
    report_ids = [key for item in items for key in item.reports]
    if len(set(report_ids)) != len(report_ids):
        raise PlanError("The replacement plans overlap.")
    for control in controls:
        parent = next((item for item in items if control["user_id"] in item.reports), None)
        if parent:
            if (control["row"].manager_upn not in {parent.attributes["userPrincipalName"].lower(),
                                                  parent.replacement["userPrincipalName"].lower()}
                    or control["record"]["identity"]["manager_id"] not in {parent.user_id, parent.replacement["id"]}):
                raise PlanError("The active direct report's feed manager is inconsistent with its approved replacement.")
            old, new = report_state(parent.reports[control["user_id"]], parent, directory)
            control["manager_id"] = parent.user_id if old else parent.replacement["id"]
        else:
            manager = directory.manager(control["row"].manager_upn)
            if manager["id"] != control["record"]["identity"].get("manager_id"):
                raise PlanError("The active employee's feed manager differs from the journal.")
            control["manager_id"] = manager["id"]
        control["before"] = control_snapshot(control, directory, groups)
        if not control["before"]["manager_correct"]:
            raise PlanError("The active employee's manager relationship differs from the journal.")
    return items, controls, notices, groups


def revocation_observed(item, user):
    revocation = item.record.get("leaver", {}).get("revocation")
    if not isinstance(revocation, dict) or revocation.get("response_outcome") == "rejected":
        return False
    after = stamp(user.get("signInSessionsValidFromDateTime"))
    before = stamp(revocation.get("baseline"))
    return after is not None and (before is None or after > before)


def verify_leaver(item, directory):
    state = snapshot(item, directory)
    reports_ok = all(report_state(report, item, directory) == (False, True)
                     for report in item.reports.values())
    return (state["user"]["accountEnabled"] is False
            and revocation_observed(item, state["user"])
            and not state["memberships"] and not state["reports"]
            and not state["licenses"]["assigned"] and state["licenses"]["states_empty"]
            and reports_ok)


def plan_events(items):
    for item in items:
        state = item.before
        base = {"target": item.row.employee_id, "user_id": item.user_id, "outcome": "planned"}
        changes = 0
        if state["user"]["accountEnabled"]:
            changes += 1
            yield {**base, "action": "disable_account"}
        if state["user"]["accountEnabled"] or not revocation_observed(item, state["user"]):
            revocation = item.record.get("leaver", {}).get("revocation")
            if revocation and revocation.get("response_outcome") in {"accepted", "unknown", "requested"} and not state["user"]["accountEnabled"]:
                yield {**base, "action": "await_revocation_verification", "outcome": "pending"}
            else:
                changes += 1
                yield {**base, "action": "revoke_sessions"}
        for name, group in item.groups.items():
            if group["id"] in state["memberships"]:
                changes += 1
                yield {**base, "action": "remove_group_membership", "group": group}
        if state["licenses"]["direct"]:
            changes += 1
            yield {**base, "action": "remove_direct_licenses", "sku_ids": sorted(state["licenses"]["direct"])}
        if state["licenses"]["inherited"]:
            yield {**base, "action": "verify_inherited_license_cleanup",
                   "source_group_ids": sorted(state["licenses"]["inherited"])}
        for report_id in sorted(state["reports"]):
            changes += 1
            yield {**base, "action": "reassign_direct_report", "report_user_id": report_id,
                   "manager": item.replacement}
        if not state["licenses"]["assigned"] and state["licenses"]["states_empty"]:
            yield {**base, "action": "license_removal", "outcome": "not_tested_no_assignment"}
        if not item.reports:
            yield {**base, "action": "direct_report_reassignment", "outcome": "not_applicable_no_reports"}
        if (not changes and revocation_observed(item, state["user"])
                and not state["licenses"]["assigned"] and state["licenses"]["states_empty"]):
            yield {**base, "action": "no_change", "outcome": "unchanged"}


def ensure_disabled(item, directory):
    user = checked_user(item, directory)
    if user["accountEnabled"]:
        raise PlanError("Account was re-enabled during cleanup. Stop, review a fresh dry run and resume.")
    return user


def perform(item, directory, journal, emit, action, method, path, body, details, check, sleep):
    operation = {"action": action, "requested_at": utc_now(), **details}
    item.record["leaver"]["operations"].append(operation)
    journal.put(item.row.employee_id, item.record)
    emit("leaver_action", target=item.row.employee_id, user_id=item.user_id,
         outcome="requested", action=action, **details)
    try:
        _, status = directory.client.write(method, path, body)
    except WriteError as error:
        operation.update(http_status=error.status, response_outcome="unknown" if error.uncertain else "rejected")
        journal.put(item.row.employee_id, item.record)
        if action == "remove_group_membership" and error.status == 404 and check():
            operation["verified_at"] = utc_now()
            journal.put(item.row.employee_id, item.record)
            emit("leaver_action_verified", target=item.row.employee_id, user_id=item.user_id,
                 action=action, outcome="already_absent", http_status=404, **details)
            return False
        raise
    operation.update(http_status=status, response_outcome="accepted")
    journal.put(item.row.employee_id, item.record)
    emit("leaver_action", target=item.row.employee_id, user_id=item.user_id,
         action=action, outcome="applied", http_status=status, **details)
    if not wait_for(check, sleep):
        raise PlanError(f"Employee {item.row.employee_id}: {action} verification is pending. Preserve the journal and resume later.")
    operation["verified_at"] = utc_now()
    journal.put(item.row.employee_id, item.record)
    emit("leaver_action_verified", target=item.row.employee_id, user_id=item.user_id,
         action=action, outcome="success", **details)
    return True


def revoke(item, directory, journal, emit, *, retry_unconfirmed):
    user = ensure_disabled(item, directory)
    state = item.record["leaver"]
    previous = state.get("revocation")
    if revocation_observed(item, user):
        if not previous.get("verified_at"):
            previous.update(verified_at=utc_now(), observed_stamp=user["signInSessionsValidFromDateTime"])
            journal.put(item.row.employee_id, item.record)
            emit("leaver_revocation_verified", target=item.row.employee_id, user_id=item.user_id,
                 outcome="desired_state_observed", sign_in_sessions_valid_from=user["signInSessionsValidFromDateTime"])
        return False
    if previous and previous.get("response_outcome") == "accepted":
        return False  # Finish other cleanup and retry read-back at final verification.
    if previous and previous.get("response_outcome") in {"requested", "unknown"} and not retry_unconfirmed:
        raise PlanError("Revocation outcome is unconfirmed. Wait and rerun a dry run; use --retry-unconfirmed-revocation only after reviewing the unresolved request.")
    if previous:
        state.setdefault("revocation_history", []).append(deepcopy(previous))
    attempt = {"requested_at": utc_now(), "baseline": user["signInSessionsValidFromDateTime"],
               "response_outcome": "requested", "explicit_retry": bool(previous and retry_unconfirmed)}
    state["revocation"] = attempt
    journal.put(item.row.employee_id, item.record)
    emit("leaver_action", target=item.row.employee_id, user_id=item.user_id,
         action="revoke_sessions", outcome="requested", explicit_retry=attempt["explicit_retry"])
    try:
        response, status = directory.client.write("POST", f"/users/{item.user_id}/revokeSignInSessions")
    except WriteError as error:
        attempt.update(http_status=error.status, response_outcome="unknown" if error.uncertain else "rejected")
        journal.put(item.row.employee_id, item.record)
        raise
    response_value = response.get("value") if isinstance(response, dict) else None
    attempt.update(http_status=status, response_outcome="accepted", response_value=response_value)
    journal.put(item.row.employee_id, item.record)
    emit("leaver_action", target=item.row.employee_id, user_id=item.user_id,
         action="revoke_sessions", outcome="applied", http_status=status, response_value=response_value)
    return True


def verify_controls(controls, directory, groups, items, emit):
    for control in controls:
        expected = deepcopy(control["before"])
        actual_control = dict(control)
        for item in items:
            if control["user_id"] in item.reports:
                actual_control["manager_id"] = item.replacement["id"]
                expected["manager_correct"] = True
        actual = control_snapshot(actual_control, directory, groups)
        if actual != expected or not actual["manager_correct"]:
            raise PlanError(f"Active employee {control['target']}: protected state changed; review before further execution.")
        emit("leaver_active_control_verified", target=control["target"], user_id=control["user_id"],
             outcome="success", account_enabled=True)
    return len(controls)


def operation_observed(item, directory, operation):
    action = operation.get("action")
    current = snapshot(item, directory)
    if action == "disable_account":
        return current["user"]["accountEnabled"] is False
    if action == "remove_group_membership":
        return operation["group"]["id"] not in current["memberships"]
    if action == "remove_direct_licenses":
        return not (set(operation["sku_ids"]) & current["licenses"]["direct"])
    if action == "reassign_direct_report":
        return report_state(item.reports[operation["report_user_id"]], item, directory) == (False, True)
    raise PlanError("An unknown operation exists in the recovery journal. Preserve the journal and review it.")


def recover_operations(item, directory, journal, emit, sleep):
    for operation in item.record["leaver"]["operations"]:
        if operation.get("verified_at"):
            continue
        action = operation["action"]
        observed = operation_observed(item, directory, operation)
        if not observed and operation.get("response_outcome") == "accepted":
            observed = wait_for(lambda: operation_observed(item, directory, operation), sleep)
            if not observed:
                raise PlanError(f"Previously accepted {action} is still awaiting verification. No duplicate write was sent; wait for propagation and review current state.")
        if observed:
            operation["verified_at"] = utc_now()
            journal.put(item.row.employee_id, item.record)
            emit("leaver_recovery", target=item.row.employee_id, user_id=item.user_id,
                 action=action, outcome="desired_state_observed_on_resume",
                 requested_at=operation["requested_at"],
                 **{key: operation[key] for key in ("group", "sku_ids", "report_user_id") if key in operation})


def apply_leavers(items, controls, groups, directory, journal, emit, *, sleep=time.sleep, retry_unconfirmed=False):
    counts = {"disabled_accounts": 0, "revocation_requests": 0, "removed_memberships": 0,
              "license_removal_requests": 0, "removed_direct_licenses": 0, "reassigned_reports": 0,
              "changed_leavers": 0, "unchanged_leavers": 0, "verified_leavers": 0}
    for item in items:
        target, record = item.row.employee_id, item.record
        before = snapshot(item, directory)
        write_start = directory.client.writes_succeeded
        if "leaver" not in record:
            record["leaver"] = {"intent": item.intent, "reports": item.reports,
                                "started_at": utc_now(), "operations": [], "revocation": None,
                                "initial_license_count": len(before["licenses"]["assigned"] | before["licenses"]["direct"]
                                                             | set().union(*before["licenses"]["inherited"].values()))}
            record["status"] = "leaving"
            journal.put(target, record)
        elif record["status"] == "left" and not verify_leaver(item, directory):
            record["status"] = "leaving"
            journal.put(target, record)
        recover_operations(item, directory, journal, emit, sleep)
        before = snapshot(item, directory)
        if before["user"]["accountEnabled"]:
            state = record["leaver"]
            if state.get("revocation"):
                state.setdefault("revocation_history", []).append(deepcopy(state["revocation"]))
            state["revocation"] = None
            record["status"] = "leaving"
            journal.put(target, record)
            if perform(item, directory, journal, emit, "disable_account", "PATCH", f"/users/{item.user_id}",
                       {"accountEnabled": False}, {}, lambda: checked_user(item, directory)["accountEnabled"] is False, sleep):
                counts["disabled_accounts"] += 1
        if revoke(item, directory, journal, emit, retry_unconfirmed=retry_unconfirmed):
            counts["revocation_requests"] += 1
        for name, group in item.groups.items():
            ensure_disabled(item, directory)
            if group["id"] not in snapshot(item, directory)["memberships"]:
                continue
            if directory.group(name)["id"] != group["id"]:
                raise PlanError("A configured group was replaced after planning.")
            if perform(item, directory, journal, emit, "remove_group_membership", "DELETE",
                       f"/groups/{group['id']}/members/{item.user_id}/$ref", None, {"group": group},
                       lambda g=group: g["id"] not in snapshot(item, directory)["memberships"], sleep):
                counts["removed_memberships"] += 1
        current = snapshot(item, directory)
        direct = sorted(current["licenses"]["direct"])
        if direct:
            ensure_disabled(item, directory)
            if perform(item, directory, journal, emit, "remove_direct_licenses", "POST",
                       f"/users/{item.user_id}/assignLicense",
                       {"addLicenses": [], "removeLicenses": direct}, {"sku_ids": direct},
                       lambda: not (set(direct) & snapshot(item, directory)["licenses"]["direct"]), sleep):
                counts["license_removal_requests"] += 1
                counts["removed_direct_licenses"] += len(direct)
        for report_id, report in item.reports.items():
            ensure_disabled(item, directory)
            old, new = report_state(report, item, directory)
            if old:
                manager = directory.manager(item.replacement["userPrincipalName"])
                if manager != item.replacement:
                    raise PlanError("The replacement manager changed after planning.")
                if perform(item, directory, journal, emit, "reassign_direct_report", "PUT",
                           f"/users/{report_id}/manager/$ref",
                           {"@odata.id": f"https://graph.microsoft.com/v1.0/users/{manager['id']}"},
                           {"report_user_id": report_id, "manager": manager},
                           lambda r=report: report_state(r, item, directory) == (False, True), sleep):
                    counts["reassigned_reports"] += 1
            report_record = deepcopy(journal.get(report["employee_key"]))
            if report_record["identity"]["manager_id"] != item.replacement["id"]:
                report_record.setdefault("manager_reassignment_history", []).append({
                    "from": report_record["identity"]["manager_id"], "to": item.replacement["id"],
                    "leaver_employee_id": target, "verified_at": utc_now()})
                report_record["identity"]["manager_id"] = item.replacement["id"]
                journal.put(report["employee_key"], report_record)
        if not wait_for(lambda: verify_leaver(item, directory), sleep):
            raise PlanError(f"Employee {target}: final verification is pending (session timestamp, group/licence propagation or manager cleanup). Account remains disabled; preserve the journal and resume later.")
        observed = checked_user(item, directory)["signInSessionsValidFromDateTime"]
        if record["status"] != "left":
            record["leaver"]["revocation"].update(verified_at=utc_now(), observed_stamp=observed)
            record["leaver"]["completed_at"] = utc_now()
            record["status"] = "left"
            journal.put(target, record)
        counts["verified_leavers"] += 1
        counts["changed_leavers" if directory.client.writes_succeeded > write_start else "unchanged_leavers"] += 1
        emit("leaver_verified", target=target, user_id=item.user_id, outcome="success",
             user_principal_name=item.attributes["userPrincipalName"], account_enabled=False,
             remaining_lab_memberships=0, remaining_assigned_licenses=0, direct_reports=0,
             sign_in_sessions_valid_from=observed,
             license_removal_test=("CLEANUP_VERIFIED" if record["leaver"]["initial_license_count"] > 0
                                   or any(op["action"] == "remove_direct_licenses" and op.get("verified_at")
                                          for op in record["leaver"]["operations"])
                                   else "NOT_TESTED_NO_ASSIGNMENT"),
             browser_session_timing="NOT_TESTED")
    counts["active_controls_verified"] = verify_controls(controls, directory, groups, items, emit)
    return counts
