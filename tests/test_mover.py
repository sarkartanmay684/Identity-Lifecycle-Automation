"""Offline HTTP-mocked mover integration and failure-recovery tests."""

from copy import deepcopy
from datetime import date
import csv
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
import requests
import responses

from test_joiner_apply import (TenantMock, FakeProtector, Config, GraphClient, GraphWriter,
                               Journal, FIXTURES, BASE, DOMAIN, TENANT, FINANCE, IT, writes, user)
from src import cli, mover_cli
from src.graph_writer import WriteError
from src.joiner_state import StateError
from src.mover_writer import MoverWriter


class MoverTenant(TenantMock):
    def __init__(self, config):
        super().__init__(config)
        hr = str(UUID(int=303))
        self.groups[hr] = {**deepcopy(next(iter(self.groups.values()))), "id": hr, "displayName": "Identity-Lifecycle-HR"}
        self.members[hr] = set()

    def callback(self, request):
        path = urlsplit(request.url).path[len("/v1.0"):]
        direct_get = request.method == "GET" and re.fullmatch(r"/users/[a-f0-9-]+", path)
        if direct_get or request.method in {"DELETE", "PATCH"}:
            body = json.loads(request.body) if request.body else None
            if self.fail:
                override = self.fail(request, path, body)
                if override is not None:
                    return override
            if direct_get:
                item = self.users.get(path.split("/")[2])
                return (200, {"Content-Type": "application/json"}, json.dumps(item)) if item else (404, {}, "missing")
            if request.method == "PATCH":
                assert set(body) <= {"department", "jobTitle"}
                self.users[path.split("/")[2]].update(body)
                return 204, {}, ""
            assert path.endswith("/$ref") and request.body is None
            group_id, identifier = path.split("/")[2], path.split("/")[4]
            if identifier not in self.members[group_id]:
                return 404, {}, "already absent"
            self.members[group_id].remove(identifier)
            return 204, {}, ""
        return super().callback(request)


@pytest.fixture
def lab(tmp_path, monkeypatch, capsys):
    config = Config(TENANT, "7181e33e-c973-4e48-8f0d-2b8ab40d5728", "fake-client-private", DOMAIN)
    state = MoverTenant(config)
    folder = tmp_path / "private-state"

    def journal_factory(cfg):
        return Journal(cfg, folder=folder, protector=FakeProtector())

    for module in (cli, mover_cli):
        monkeypatch.setattr(module, "load_config", lambda: config)
        monkeypatch.setattr(module, "Journal", journal_factory)
        monkeypatch.setattr(module, "GraphClient", lambda cfg: GraphClient(cfg, app=state.app, sleep=state.sleep))
    monkeypatch.setattr(cli, "GraphWriter", lambda cfg: GraphWriter(cfg, app=state.app, sleep=state.sleep))
    monkeypatch.setattr(mover_cli, "MoverWriter", lambda cfg: MoverWriter(cfg, app=state.app, sleep=state.sleep))
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        for method in (responses.GET, responses.POST, responses.PUT, responses.DELETE, responses.PATCH):
            http.add_callback(method, re.compile(re.escape(BASE) + r"/.*"), callback=state.callback)
        assert cli.main(["joiner", "--apply", "--csv", str(FIXTURES / "hr_feed.csv"),
                         "--group-map", str(FIXTURES / "group_map.json")]) == 0
        capsys.readouterr()
        mover_file = tmp_path / "hr_feed_mover.csv"
        with (FIXTURES / "hr_feed.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        rows[0].update(department="IT", job_title="IT Analyst", manager_upn="it.manager@" + DOMAIN)
        save_rows(mover_file, rows)
        journal = journal_factory(config).load()
        state.riya = journal.get("ILAB-001")["user_id"]
        state.arjun = journal.get("ILAB-002")["user_id"]
        state.group_id = lambda suffix: next(key for key, value in state.groups.items() if value["displayName"].endswith(suffix))
        yield state, http, journal, mover_file, rows


def save_rows(path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run(lab, capsys, mode, *extra):
    code = cli.main(["mover", mode, "--csv", str(lab[3]), "--group-map", str(FIXTURES / "group_map.json"), *extra])
    output = capsys.readouterr().out
    return code, [json.loads(line) for line in output.splitlines()], output


def test_dry_run_detects_active_department_diff_without_any_write(lab, capsys):
    state, http, journal, _, _ = lab
    before, data, count = deepcopy(state.users), journal.path.read_bytes(), len(writes(http))
    code, events, _ = run(lab, capsys, "--dry-run")
    assert code == 0
    assert events[-1]["actions"] == {"department_change": 1, "remove_group_membership": 1,
                                    "add_group_membership": 1, "update_attributes": 1, "set_manager": 1, "no_change": 1}
    assert events[-1]["directory_writes"] == 0 and events[-1]["planned_directory_writes"] == 4
    assert next(event for event in events if event.get("action") == "department_change")["csv_status"] == "active"
    assert len(writes(http)) == count and journal.path.read_bytes() == data and state.users == before
    assert not journal.lock_path.exists()


def test_apply_removes_first_logs_removal_and_preserves_ids_passwords_and_unmanaged_access(lab, capsys):
    state, http, journal, _, _ = lab
    extra = str(UUID(int=850))
    state.groups[extra] = {**deepcopy(next(iter(state.groups.values()))), "id": extra, "displayName": "Unmanaged-Project"}
    state.members[extra] = {state.riya}
    before_ids, passwords, before_arjun = set(state.users), list(state.passwords), deepcopy(state.users[state.arjun])
    count = len(writes(http))
    code, events, output = run(lab, capsys, "--apply")
    assert code == 0
    summary = events[-1]
    assert {key: summary[key] for key in ("changed_users", "unchanged_users", "removed_memberships", "added_memberships",
                                         "updated_users", "assigned_managers", "verified_users", "directory_writes")} == {
        "changed_users": 1, "unchanged_users": 1, "removed_memberships": 1, "added_memberships": 1,
        "updated_users": 1, "assigned_managers": 1, "verified_users": 2, "directory_writes": 4}
    assert [call.request.method for call in writes(http)[count:]] == ["DELETE", "POST", "PATCH", "PUT"]
    deletion = writes(http)[count].request
    assert deletion.url.endswith(f"/members/{state.riya}/$ref") and deletion.body is None
    removed = next(event for event in events if event.get("action") == "remove_group_membership" and event["outcome"] == "applied")
    assert removed["actor"] == state.config.client_id and removed["target"] == "ILAB-001"
    assert removed["timestamp"].endswith("+00:00") and removed["group"]["displayName"].endswith("Finance")
    assert removed["http_status"] == 204
    assert state.users[state.riya]["department"] == "IT" and state.users[state.riya]["manager_id"] == IT
    assert state.riya not in state.members[state.group_id("Finance")]
    assert state.riya in state.members[state.group_id("IT")] and state.riya in state.members[state.group_id("All-Employees")]
    assert state.riya in state.members[extra] and state.users[state.arjun] == before_arjun
    assert set(state.users) == before_ids and state.passwords == passwords
    assert all(password not in output for password in passwords)
    assert "mock-token-private" not in output and "fake-client-private" not in output
    record = journal.load().get("ILAB-001")
    assert record["status"] == "complete" and record["identity"]["attributes"]["department"] == "IT"
    assert record["initial_identity"]["attributes"]["department"] == "Finance" and len(record["mover_history"]) == 1


def test_verify_before_move_fails_without_mutation(lab, capsys):
    _, http, journal, _, _ = lab
    data, count = journal.path.read_bytes(), len(writes(http))
    code, events, _ = run(lab, capsys, "--verify")
    assert code == 1 and events[-1]["verified_users"] == 1 and events[-1]["directory_writes"] == 0
    assert journal.path.read_bytes() == data and len(writes(http)) == count


def test_verify_and_unchanged_apply_make_zero_writes_and_keep_state(lab, capsys):
    state, http, journal, _, _ = lab
    assert run(lab, capsys, "--apply")[0] == 0
    data, count, before = journal.path.read_bytes(), len(writes(http)), deepcopy(state.users)
    assert run(lab, capsys, "--verify")[0] == 0
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["directory_writes"] == 0 and events[-1]["unchanged_users"] == 2
    assert events[-1]["verified_users"] == 2 and state.users == before
    assert len(writes(http)) == count and journal.path.read_bytes() == data


@pytest.mark.parametrize("method", ["DELETE", "POST", "PATCH", "PUT"])
def test_failure_stops_then_resumes_without_recreating_users(lab, capsys, method):
    state, http, journal, _, _ = lab
    before, passwords = set(state.users), list(state.passwords)
    count = len(writes(http))
    state.fail = lambda req, path, body: (403, {}, "secret error body") if req.method == method else None
    code, events, output = run(lab, capsys, "--apply")
    assert code == 1 and events[-1]["outcome"] == "failure" and "secret error body" not in output
    attempted = writes(http)[count:]
    assert attempted[-1].request.method == method
    if method == "DELETE":
        assert len(attempted) == 1 and state.riya not in state.members[state.group_id("IT")]
    assert journal.load().get("ILAB-001")["status"] == "moving"
    state.fail = None
    assert run(lab, capsys, "--apply")[0] == 0
    assert run(lab, capsys, "--verify")[0] == 0
    assert set(state.users) == before and state.passwords == passwords


def test_timeout_after_removal_commit_is_verified_and_not_deleted_twice(lab, capsys):
    state, http, journal, _, _ = lab
    def timeout(req, path, body):
        if req.method == "DELETE":
            state.members[state.group_id("Finance")].remove(state.riya)
            raise requests.Timeout()
    state.fail = timeout
    assert run(lab, capsys, "--apply")[0] == 1
    state.fail = None
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["removed_memberships"] == 0 and events[-1]["directory_writes"] == 3
    assert len([call for call in http.calls if call.request.method == "DELETE"]) == 1
    recovered = next(event for event in events if event["event"] == "mover_recovery")
    assert recovered["action"] == "remove_group_membership" and recovered["outcome"] == "desired_state_observed_on_resume"


def test_429_retries_with_retry_after(lab, capsys):
    state, _, _, _, _ = lab
    seen = []
    def throttle(req, path, body):
        if req.method == "DELETE" and not seen:
            seen.append(True)
            return 429, {"Retry-After": "7"}, "throttled"
    state.fail = throttle
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["write_attempts"] == 5
    state.sleep.assert_any_call(7.0)


def test_500_is_not_automatically_replayed(lab, capsys):
    state, http, _, _, _ = lab
    count = len(writes(http))
    state.fail = lambda req, path, body: (500, {}, "private") if req.method == "DELETE" else None
    assert run(lab, capsys, "--apply")[0] == 1 and len(writes(http)) == count + 1


@pytest.mark.parametrize("change", [{"accountEnabled": False}, {"onPremisesSyncEnabled": True},
                                   {"onPremisesImmutableId": "linked"}, {"userType": "Guest"},
                                   {"employeeId": "OTHER-001"}, {"userPrincipalName": "other@outside.example"}])
def test_out_of_scope_employee_blocks_entire_batch(lab, capsys, change):
    state, http, _, _, _ = lab
    state.users[state.arjun].update(change)
    count = len(writes(http))
    assert run(lab, capsys, "--apply")[0] == 1 and len(writes(http)) == count


def test_admin_and_duplicate_identity_are_rejected(lab, capsys):
    state, http, _, _, _ = lab
    count = len(writes(http))
    state.roles[state.arjun] = [{"@odata.type": "#microsoft.graph.directoryRole", "id": str(UUID(int=901))}]
    assert run(lab, capsys, "--apply")[0] == 1 and len(writes(http)) == count
    state.roles.clear()
    state.users[str(UUID(int=902))] = user(str(UUID(int=902)), "duplicate@" + DOMAIN, employeeId="ILAB-002")
    assert run(lab, capsys, "--apply")[0] == 1 and len(writes(http)) == count


@pytest.mark.parametrize("change", [{"isAssignableToRole": True}, {"groupTypes": ["DynamicMembership"]},
                                   {"onPremisesSyncEnabled": True}, {"mailEnabled": True}])
def test_unsafe_mapped_group_blocks_batch(lab, capsys, change):
    state, http, _, _, _ = lab
    state.groups[state.group_id("Finance")].update(change)
    count = len(writes(http))
    assert run(lab, capsys, "--apply")[0] == 1 and len(writes(http)) == count


def test_missing_journal_and_missing_user_never_trigger_creation(lab, capsys):
    state, http, journal, _, _ = lab
    count, backup = len(writes(http)), journal.path.read_bytes()
    journal.path.unlink()
    assert run(lab, capsys, "--apply")[0] == 1 and len(writes(http)) == count
    journal.path.write_bytes(backup)
    del state.users[state.riya]
    assert run(lab, capsys, "--apply")[0] == 1 and len(writes(http)) == count


def test_changed_csv_during_partial_move_is_rejected(lab, capsys):
    state, http, _, path, rows = lab
    state.fail = lambda req, url, body: (403, {}, "denied") if req.method == "PATCH" else None
    assert run(lab, capsys, "--apply")[0] == 1
    state.fail = None
    rows[0]["job_title"] = "Changed Midflight"
    save_rows(path, rows)
    count = len(writes(http))
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 1 and "different inputs" in events[-1]["message"] and len(writes(http)) == count


def test_changed_mapping_id_during_partial_move_is_rejected(lab, capsys):
    state, http, _, _, _ = lab
    state.fail = lambda req, url, body: (403, {}, "denied") if req.method == "DELETE" else None
    assert run(lab, capsys, "--apply")[0] == 1
    state.fail = None
    old_id, new_id = state.group_id("IT"), str(UUID(int=905))
    state.groups[new_id] = {**state.groups.pop(old_id), "id": new_id}
    state.members[new_id] = state.members.pop(old_id)
    count = len(writes(http))
    assert run(lab, capsys, "--apply")[0] == 1 and len(writes(http)) == count


def test_local_persistence_failure_happens_before_removal(lab, capsys, monkeypatch):
    _, http, _, _, _ = lab
    count = len(writes(http))
    def broken(self, data):
        raise StateError("Storage unavailable")
    monkeypatch.setattr(FakeProtector, "protect", broken)
    assert run(lab, capsys, "--apply")[0] == 1 and len(writes(http)) == count


def test_same_department_stale_access_is_removed_and_common_group_repaired(lab, capsys):
    state, _, _, _, _ = lab
    assert run(lab, capsys, "--apply")[0] == 0
    state.members[state.group_id("HR")].add(state.riya)
    state.members[state.group_id("All-Employees")].remove(state.riya)
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["removed_memberships"] == 1 and events[-1]["added_memberships"] == 1
    assert events[-1]["updated_users"] == 0 and events[-1]["assigned_managers"] == 0


def test_collection_visibility_delay_does_not_break_direct_id_reads(lab, capsys):
    state, _, _, _, _ = lab
    state.fail = lambda req, path, body: (200, {}, '{"value": []}') if (req.method == "GET" and path == "/users"
        and parse_qs(urlsplit(req.url).query).get("$filter", [""])[0].startswith("employeeId eq")) else None
    assert run(lab, capsys, "--apply")[0] == 0


def test_terminated_input_blocks_all_mover_writes(lab, capsys):
    _, http, _, path, rows = lab
    rows[1].update(status="terminated", end_date="2026-10-25")
    save_rows(path, rows)
    count = len(writes(http))
    assert run(lab, capsys, "--apply")[0] == 1 and len(writes(http)) == count


def test_future_start_is_deferred_without_changes(lab, capsys):
    _, http, _, path, rows = lab
    rows[0]["start_date"] = "2099-01-01"
    save_rows(path, rows)
    count = len(writes(http))
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["deferred_users"] == 1 and events[-1]["directory_writes"] == 0
    assert len(writes(http)) == count


@pytest.mark.parametrize("args", [["mover"], ["mover", "--apply"],
    ["mover", "--apply", "--verify", "--csv", "anything.csv"],
    ["mover", "--apply", "--csv", "anything.csv", "--as-of", "2026-10-25"]])
def test_cli_requires_explicit_mode_and_feed(args):
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2


@pytest.mark.parametrize("method,path,body", [
    ("DELETE", "/users/00000000-0000-0000-0000-000000000001", None),
    ("DELETE", "/groups/00000000-0000-0000-0000-000000000001/members/00000000-0000-0000-0000-000000000002", None),
    ("POST", "/users", {"displayName": "Not allowed"}),
    ("PATCH", "/users/00000000-0000-0000-0000-000000000001", {"accountEnabled": False}),
    ("POST", "https://outside.example/anything", {}),
])
def test_transport_rejects_unsafe_operations_before_network(lab, method, path, body):
    state, http, _, _, _ = lab
    count = len(http.calls)
    with MoverWriter(state.config, app=state.app) as client:
        with pytest.raises(WriteError, match="outside"):
            client.write(method, path, body)
    assert len(http.calls) == count


def test_stale_joiner_feed_cannot_reverse_completed_move(lab, capsys):
    _, http, _, _, _ = lab
    assert run(lab, capsys, "--apply")[0] == 0
    count = len(writes(http))
    assert cli.main(["joiner", "--apply", "--csv", str(FIXTURES / "hr_feed.csv"),
                     "--group-map", str(FIXTURES / "group_map.json")]) == 1
    assert len(writes(http)) == count
    capsys.readouterr()


def test_audit_file_retains_removal_and_appends_rerun_without_secrets(lab, capsys, tmp_path):
    state, _, _, _, _ = lab
    path = tmp_path / "logs" / "mover.jsonl"
    assert run(lab, capsys, "--apply", "--log-file", str(path))[0] == 0
    original = path.read_bytes()
    events = [json.loads(line) for line in original.decode("utf-8").splitlines()]
    assert any(event.get("action") == "remove_group_membership" and event["outcome"] == "applied" for event in events)
    assert all(password.encode() not in original for password in state.passwords)
    assert run(lab, capsys, "--apply", "--log-file", str(path))[0] == 0
    assert path.read_bytes().startswith(original) and len(path.read_bytes()) > len(original)


def test_unwritable_audit_destination_stops_before_graph_changes(lab, capsys, tmp_path):
    _, http, _, _, _ = lab
    path = tmp_path / "directory.jsonl"
    path.mkdir()
    count = len(writes(http))
    code, events, _ = run(lab, capsys, "--apply", "--log-file", str(path))
    assert code == 1 and "audit" in events[-1]["message"] and len(writes(http)) == count


def test_dry_run_rejects_audit_file_without_creating_it(lab, capsys, tmp_path):
    path = tmp_path / "new-log.jsonl"
    with pytest.raises(SystemExit):
        run(lab, capsys, "--dry-run", "--log-file", str(path))
    assert not path.exists()


def test_scoped_transport_still_rejects_object_delete_and_account_patch(lab):
    state, http, _, _, _ = lab
    count = len(http.calls)
    with MoverWriter(state.config, app=state.app) as client:
        client.users = {state.riya}
        client.groups = set(state.groups)
        client.managers = {IT}
        with pytest.raises(WriteError):
            client.write("DELETE", f"/groups/{state.group_id('Finance')}/members/{state.riya}")
        with pytest.raises(WriteError):
            client.write("PATCH", f"/users/{state.riya}", {"accountEnabled": False})
    assert len(http.calls) == count


def test_removal_visibility_timeout_stops_before_grant_and_resumes(lab, capsys):
    state, http, journal, _, _ = lab
    marker = {"deleted": False}
    finance = state.group_id("Finance")
    def delayed(req, path, body):
        if req.method == "DELETE":
            marker["deleted"] = True
        elif marker["deleted"] and req.method == "GET" and path == f"/groups/{finance}/members":
            return 200, {}, json.dumps({"value": [{"id": state.riya}]})
    state.fail = delayed
    count = len(writes(http))
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 1 and "not yet visible" in events[-1]["message"]
    assert len(writes(http)) == count + 1 and state.riya not in state.members[state.group_id("IT")]
    state.sleep.assert_any_call(16)
    assert journal.load().get("ILAB-001")["status"] == "moving"
    state.fail = None
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["directory_writes"] == 3
    assert len([call for call in http.calls if call.request.method == "DELETE"]) == 1
