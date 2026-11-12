"""Offline tests. The HTTP mock rejects any unexpected or mutating request."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
import responses

from src.config import Config
from src.graph_client import GraphClient
from src.hr_feed import COLUMNS, InputError, load_feed, load_group_map
from src.joiner_plan import DirectoryReader, PlanError, build_joiner_plan
from src import preview_joiners


FIXTURES = Path(__file__).parent / "fixtures" / "joiner_preview"
TODAY = date(2026, 10, 16)
TENANT = "95d4a545-0037-4e61-860b-c90d2952b03b"
DOMAIN = "hybrididentity.site"
MANAGERS = {
    "finance.manager@hybrididentity.site": "324a59b8-d955-4df2-a55c-637c8ecafe29",
    "it.manager@hybrididentity.site": "6c0e9049-d25e-4f03-8a5a-3ad8fb342493",
}
GROUPS = {
    "Identity-Lifecycle-All-Employees": "dc405ef1-c7be-45df-b30b-758f9033b9e6",
    "Identity-Lifecycle-Finance": "69e681b1-1132-454c-b678-cf809f3d815e",
    "Identity-Lifecycle-IT": "3669e3ba-cec1-4cb0-ba5e-7f751467e9d3",
}


def user(upn, identifier, **extra):
    return {
        "id": identifier, "userPrincipalName": upn, "employeeId": None,
        "userType": "Member", "onPremisesSyncEnabled": None,
        "onPremisesImmutableId": None, "accountEnabled": True,
        "department": "Finance", "jobTitle": "Finance Analyst", **extra,
    }


@pytest.fixture
def inputs():
    mapping = load_group_map(FIXTURES / "group_map.json")
    return load_feed(FIXTURES / "hr_feed.csv", mapping), mapping


@pytest.fixture
def directory():
    config = Config(TENANT, "7181e33e-c973-4e48-8f0d-2b8ab40d5728", "unit-test-placeholder", DOMAIN)
    manager_users = [user(upn, identifier) for upn, identifier in MANAGERS.items()]
    group_objects = [{
        "id": identifier, "displayName": name, "securityEnabled": True,
        "mailEnabled": False, "groupTypes": [], "isAssignableToRole": False,
        "onPremisesSyncEnabled": None,
    } for name, identifier in GROUPS.items()]
    state = SimpleNamespace(
        users=manager_users, groups=group_objects, memberships={}, calls=[],
        organizations=[{"id": TENANT, "displayName": "Hybrid_Identity", "verifiedDomains": [{"name": DOMAIN}]}],
    )

    def get_all(path, *, params=None):
        state.calls.append((path, params))
        if path == "/organization":
            return deepcopy(state.organizations)
        if path in {"/users", "/groups"}:
            field, text = params["$filter"].split(" eq ", 1)
            value = text[1:-1].replace("''", "'")
            collection = state.users if path == "/users" else state.groups
            return deepcopy([item for item in collection if str(item.get(field, "")).casefold() == value.casefold()])
        if path.endswith("/transitiveMemberOf"):
            return deepcopy(state.memberships.get(path.split("/")[2], []))
        raise AssertionError(f"Unexpected request: {path}")

    client = Mock(spec=["get_all"])
    client.get_all.side_effect = get_all
    return DirectoryReader(client, config), state


def test_uploaded_feed_and_mapping_validate(inputs):
    employees, mapping = inputs
    assert [row.employee_id for row in employees] == ["ILAB-001", "ILAB-002"]
    assert mapping.groups_for("Finance") == ("Identity-Lifecycle-All-Employees", "Identity-Lifecycle-Finance")


@pytest.mark.parametrize("old,new", [
    ("ILAB-002", "ILAB-001"),
    ("ILAB-001", "OTHER-001"),
    ("ILAB-001", "ILAB-1234567890123"),
    ("Finance Analyst", ""),
    ("2026-10-02", "2026-02-30"),
    ("2026-10-02", "10/02/2026"),
    (",,active", ",2026-10-16,active"),
    (",,active", ",,terminated"),
    (",,active", ",2026-10-01,terminated"),
    (",,active", ",,inactive"),
    (",Finance,", ",Unknown,"),
    ("finance.manager@hybrididentity.site", "not-a-upn"),
    ("employee_id,first_name", "employee_id,employee_id"),
])
def test_bad_csv_rejected_with_useful_error(tmp_path, inputs, old, new):
    text = (FIXTURES / "hr_feed.csv").read_text()
    path = tmp_path / "bad.csv"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(InputError):
        load_feed(path, inputs[1])


@pytest.mark.parametrize("body", ["", ",".join(COLUMNS) + "\n"])
def test_empty_feed_does_not_mean_termination(tmp_path, inputs, body):
    path = tmp_path / "empty.csv"
    path.write_text(body)
    with pytest.raises(InputError):
        load_feed(path, inputs[1])


def test_bom_csv_and_quoted_names_supported(tmp_path, inputs):
    path = tmp_path / "bom.csv"
    text = (FIXTURES / "hr_feed.csv").read_text().replace("Riya", '"Riya, Anne"', 1)
    path.write_text(text, encoding="utf-8-sig")
    assert load_feed(path, inputs[1])[0].first_name == "Riya, Anne"


@pytest.mark.parametrize("change", [
    lambda data: data.update(employee_id_prefix=""),
    lambda data: data.update(common_groups=[]),
    lambda data: data.update(common_groups=["Duplicate", "duplicate"]),
    lambda data: data.update(department_groups=[]),
])
def test_mapping_rejects_invalid_scope_or_groups(tmp_path, change):
    data = json.loads((FIXTURES / "group_map.json").read_text())
    change(data)
    path = tmp_path / "map.json"
    path.write_text(json.dumps(data))
    with pytest.raises(InputError):
        load_group_map(path)


def test_duplicate_json_keys_rejected(tmp_path):
    path = tmp_path / "map.json"
    path.write_text('{"employee_id_prefix":"ILAB-", "employee_id_prefix":"OTHER-"}')
    with pytest.raises(InputError, match="Duplicate JSON key"):
        load_group_map(path)


def test_new_joiners_have_correct_groups_managers_and_password_policy(inputs, directory):
    employees, mapping = inputs
    reader, state = directory
    original = deepcopy(state.users)
    plan = build_joiner_plan(employees, mapping, reader, TODAY)
    assert len(plan) == 8
    for employee, department, manager in [
        ("ILAB-001", "Finance", "finance.manager@hybrididentity.site"),
        ("ILAB-002", "IT", "it.manager@hybrididentity.site"),
    ]:
        actions = [item for item in plan if item["target"] == employee]
        assert [item["action"] for item in actions] == ["create_user", "add_group_membership", "add_group_membership", "set_manager"]
        assert actions[0]["attributes"]["accountEnabled"] is True
        assert actions[0]["password_policy"]["forceChangePasswordNextSignIn"] is True
        assert [item["group"]["displayName"] for item in actions[1:3]] == [
            "Identity-Lifecycle-All-Employees", "Identity-Lifecycle-" + department,
        ]
        assert actions[3]["manager"]["id"] == MANAGERS[manager]
    assert state.users == original
    assert '"password":' not in json.dumps(plan)


def test_upn_collisions_and_same_name_rows_are_deterministic(inputs, directory):
    employees, mapping = inputs
    reader, state = directory
    state.users.append(user("riya.shah@" + DOMAIN, str(UUID(int=100))))
    same_names = [employees[0], replace(employees[1], first_name="Riya", last_name="Shah")]
    def upns(rows):
        return [item["attributes"]["userPrincipalName"] for item in
                build_joiner_plan(rows, mapping, reader, TODAY) if item["action"] == "create_user"]
    assert upns(same_names) == ["riya.shah2@" + DOMAIN, "riya.shah3@" + DOMAIN]
    assert upns(list(reversed(same_names))) == upns(same_names)


def test_existing_disabled_employee_is_never_recreated_or_enabled(inputs, directory):
    employees, mapping = inputs
    reader, state = directory
    state.users.append(user("old.name@" + DOMAIN, str(UUID(int=101)), employeeId="ILAB-001", accountEnabled=False))
    plan = build_joiner_plan(employees, mapping, reader, TODAY)
    riya = [item for item in plan if item["target"] == "ILAB-001"]
    assert len(riya) == 1
    assert riya[0]["action"] == "skip"
    assert riya[0]["reason"] == "existing_account_requires_reconciliation"
    assert riya[0]["account_enabled"] is False


def test_department_difference_detected_even_with_active_status(inputs, directory):
    employees, mapping = inputs
    reader, state = directory
    state.users.append(user("riya.shah@" + DOMAIN, str(UUID(int=101)), employeeId="ILAB-001", department="IT"))
    plan = build_joiner_plan(employees, mapping, reader, TODAY)
    assert plan[0]["department_changed"] is True
    assert plan[0]["outcome"] == "pending"


def test_duplicate_directory_employee_id_aborts_plan(inputs, directory):
    reader, state = directory
    state.users.extend([user(f"duplicate{index}@{DOMAIN}", str(UUID(int=index)), employeeId="ILAB-001") for index in (101, 102)])
    with pytest.raises(PlanError, match="multiple directory users"):
        build_joiner_plan(*inputs, reader, TODAY)


@pytest.mark.parametrize("change,match", [
    ({"onPremisesSyncEnabled": True}, "synchronized"),
    ({"onPremisesImmutableId": "previous-sync-marker"}, "synchronized"),
    ({"userType": "Guest"}, "Member"),
    ({"accountEnabled": False}, "enabled"),
])
def test_unsafe_manager_aborts_plan(inputs, directory, change, match):
    reader, state = directory
    state.users[0].update(change)
    with pytest.raises(PlanError, match=match):
        build_joiner_plan(*inputs, reader, TODAY)


def test_active_administrator_manager_excluded_with_limited_role_data(inputs, directory):
    reader, state = directory
    state.memberships[MANAGERS["finance.manager@" + DOMAIN]] = [
        {"id": str(UUID(int=501)), "@odata.type": "#microsoft.graph.directoryRole", "displayName": None}
    ]
    with pytest.raises(PlanError, match="directory-role"):
        build_joiner_plan(*inputs, reader, TODAY)


@pytest.mark.parametrize("change", [
    {"isAssignableToRole": True}, {"groupTypes": ["DynamicMembership"]},
    {"onPremisesSyncEnabled": True}, {"securityEnabled": False}, {"mailEnabled": True},
])
def test_unsafe_groups_excluded(inputs, directory, change):
    reader, state = directory
    state.groups[0].update(change)
    with pytest.raises(PlanError, match="Assigned security group"):
        build_joiner_plan(*inputs, reader, TODAY)


def test_ambiguous_group_name_rejected(inputs, directory):
    reader, state = directory
    state.groups.append({**state.groups[0], "id": str(UUID(int=303))})
    with pytest.raises(PlanError, match="exactly one match"):
        build_joiner_plan(*inputs, reader, TODAY)


@pytest.mark.parametrize("change", ["tenant", "domain"])
def test_wrong_tenant_or_domain_stops_before_employee_queries(inputs, directory, change):
    reader, state = directory
    if change == "tenant":
        state.organizations[0]["id"] = str(UUID(int=999))
    else:
        state.organizations[0]["verifiedDomains"] = []
    with pytest.raises(PlanError):
        build_joiner_plan(*inputs, reader, TODAY)
    assert len(state.calls) == 1


@pytest.mark.parametrize("status,start,end,reason", [
    ("active", date(2026, 10, 17), None, "future_start"),
    ("terminated", date(2026, 10, 2), date(2026, 10, 17), "future_termination"),
    ("terminated", date(2026, 10, 2), TODAY, "terminated_employee_not_found"),
])
def test_scheduling_and_termination_never_create_uneligible_users(inputs, directory, status, start, end, reason):
    employees, mapping = inputs
    row = replace(employees[0], status=status, start_date=start, end_date=end)
    plan = build_joiner_plan([row], mapping, directory[0], TODAY)
    assert plan == [{"action": "skip", "target": "ILAB-001", "outcome": "skipped" if end == TODAY else "deferred",
                     "reason": reason, **({"effective_date": (end or start).isoformat()} if end != TODAY else {})}]


def test_existing_termination_reported_pending_even_if_already_disabled(inputs, directory):
    employees, mapping = inputs
    reader, state = directory
    state.users.append(user("riya.shah@" + DOMAIN, str(UUID(int=101)), employeeId="ILAB-001", accountEnabled=False))
    row = replace(employees[0], status="terminated", end_date=TODAY)
    plan = build_joiner_plan([row], mapping, reader, TODAY)
    assert plan[0]["reason"] == "leaver_not_implemented"
    assert plan[0]["outcome"] == "pending"


def test_transferred_missing_employee_is_eligible_joiner(inputs, directory):
    employees, mapping = inputs
    plan = build_joiner_plan([replace(employees[0], status="transferred")], mapping, directory[0], TODAY)
    assert plan[0]["action"] == "create_user"


def test_validation_only_never_loads_credentials_or_graph(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Validation-only must not authenticate or use Graph")
    monkeypatch.setattr(preview_joiners, "load_config", forbidden)
    monkeypatch.setattr(preview_joiners, "GraphClient", forbidden)
    code = preview_joiners.main(["--validate-only", "--csv", str(FIXTURES / "hr_feed.csv"),
                                "--group-map", str(FIXTURES / "group_map.json")])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["employees"] == 2 and result["graph_requests"] == 0


@pytest.mark.parametrize("args", [[], ["--apply"], ["--dry-run", "--validate-only"]])
def test_cli_has_no_apply_mode_and_requires_explicit_mode(args):
    with pytest.raises(SystemExit) as error:
        preview_joiners.main(args)
    assert error.value.code == 2


def test_directory_failure_emits_no_partial_actions(monkeypatch, capsys, directory):
    reader, state = directory
    # The first employee is eligible, but the second has duplicate IDs in Graph.
    # No action for the first employee should escape before this failure.
    state.users.extend([user(f"duplicate{index}@{DOMAIN}", str(UUID(int=index)), employeeId="ILAB-002")
                        for index in (101, 102)])
    monkeypatch.setattr(preview_joiners, "load_config", lambda: reader.config)
    client = Mock()
    client.__enter__ = Mock(return_value=reader.client)
    client.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(preview_joiners, "GraphClient", lambda config: client)
    code = preview_joiners.main(["--dry-run", "--csv", str(FIXTURES / "hr_feed.csv"),
                                "--group-map", str(FIXTURES / "group_map.json")])
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 1 and len(events) == 1
    assert events[0]["event"] == "joiner_preview_error"
    assert events[0]["directory_writes"] == 0


@responses.activate
def test_entire_cli_dry_run_uses_only_graph_gets(monkeypatch, capsys, inputs, directory):
    reader, state = directory
    from urllib.parse import parse_qs, urlsplit
    base = "https://graph.microsoft.com/v1.0"
    def callback(request):
        parsed = urlsplit(request.url)
        path = parsed.path[len("/v1.0"):]
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        if path == "/organization":
            payload = state.organizations
        elif path in {"/users", "/groups"}:
            field, literal = params["$filter"].split(" eq ", 1)
            value = literal[1:-1].replace("''", "'")
            items = state.users if path == "/users" else state.groups
            payload = [item for item in items if str(item.get(field, "")).casefold() == value.casefold()]
        else:
            payload = []
        return 200, {"Content-Type": "application/json"}, json.dumps({"value": payload})
    for path in ["/organization", "/users", "/groups"] + [f"/users/{identifier}/transitiveMemberOf" for identifier in MANAGERS.values()]:
        responses.add_callback(responses.GET, base + path, callback=callback)
    app = Mock()
    app.acquire_token_for_client.return_value = {"access_token": "test-only-token"}
    monkeypatch.setattr(preview_joiners, "load_config", lambda: reader.config)
    monkeypatch.setattr(preview_joiners, "GraphClient", lambda config: GraphClient(config, app=app))
    before = deepcopy(state.users)
    code = preview_joiners.main(["--dry-run", "--csv", str(FIXTURES / "hr_feed.csv"),
                                "--group-map", str(FIXTURES / "group_map.json"), "--as-of", "2026-10-16"])
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0 and len(events) == 9
    assert all(call.request.method == "GET" for call in responses.calls)
    assert state.users == before
    assert events[-1]["actions"] == {"create_user": 2, "add_group_membership": 4, "set_manager": 2, "skip": 0}
    assert all(event["read_only"] and event["dry_run"] for event in events)
    assert "test-only-token" not in json.dumps(events)
    assert "unit-test-placeholder" not in json.dumps(events)
