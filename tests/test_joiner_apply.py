"""Offline apply/verification tests. Real DPAPI round-trip runs only on Windows."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
import os
from pathlib import Path
import re
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
import requests
import responses

from src import cli
from src.config import Config
from src.graph_client import GraphClient
from src.graph_writer import GraphWriter, WriteError
from src.hr_feed import load_feed, load_group_map
from src.joiner import JoinerDirectory, new_password, prepare
from src.joiner_plan import PlanError
from src.joiner_state import Journal, StateError, WindowsProtector


FIXTURES = Path(__file__).parent / "fixtures" / "joiner_preview"
BASE = "https://graph.microsoft.com/v1.0"
DOMAIN = "hybrididentity.site"
TENANT = "95d4a545-0037-4e61-860b-c90d2952b03b"
TODAY = date(2026, 10, 16)
FINANCE = "324a59b8-d955-4df2-a55c-637c8ecafe29"
IT = "6c0e9049-d25e-4f03-8a5a-3ad8fb342493"
GROUP_NAMES = ["Identity-Lifecycle-All-Employees", "Identity-Lifecycle-Finance", "Identity-Lifecycle-IT"]


class FakeProtector:
    """Test double only: deliberately not cryptography; never used by product code."""
    def protect(self, data):
        return b"TEST-DOUBLE:" + data[::-1]

    def unprotect(self, data):
        assert data.startswith(b"TEST-DOUBLE:")
        return data[len(b"TEST-DOUBLE:"):][::-1]

    def self_check(self):
        pass


def user(identifier, upn, **attrs):
    return {"id": identifier, "userPrincipalName": upn, "employeeId": None, "userType": "Member",
            "accountEnabled": True, "onPremisesSyncEnabled": None, "onPremisesImmutableId": None,
            "givenName": "Manager", "surname": "Test", "displayName": "Manager Test",
            "department": "Finance", "jobTitle": "Manager", "mailNickname": upn.split("@")[0], **attrs}


class TenantMock:
    def __init__(self, config):
        self.config = config
        self.users = {FINANCE: user(FINANCE, "finance.manager@" + DOMAIN), IT: user(IT, "it.manager@" + DOMAIN)}
        self.groups = {str(UUID(int=300+index)): {"id": str(UUID(int=300+index)), "displayName": name,
                      "securityEnabled": True, "mailEnabled": False, "groupTypes": [],
                      "isAssignableToRole": False, "onPremisesSyncEnabled": None}
                      for index, name in enumerate(GROUP_NAMES)}
        self.members = {identifier: set() for identifier in self.groups}
        self.roles = {}
        self.passwords = []
        self.posted_profiles = []
        self.fail = None
        self.counter = 1000
        self.sleep = Mock()
        self.app = Mock()
        self.app.acquire_token_for_client.return_value = {"access_token": "mock-token-private"}

    def callback(self, request):
        parsed = urlsplit(request.url)
        path = parsed.path[len("/v1.0"):]
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        body = json.loads(request.body) if request.body else None
        if self.fail:
            override = self.fail(request, path, body)
            if override is not None:
                return override
        value = None
        if request.method == "GET":
            if path == "/organization":
                value = [{"id": TENANT, "displayName": "Hybrid_Identity", "verifiedDomains": [{"name": DOMAIN}]}]
            elif path in {"/users", "/groups"}:
                field, literal = params["$filter"].split(" eq ", 1)
                expected = literal[1:-1].replace("''", "'")
                objects = self.users.values() if path == "/users" else self.groups.values()
                value = [item for item in objects if str(item.get(field, "")).casefold() == expected.casefold()]
            elif path.endswith("/transitiveMemberOf"):
                value = self.roles.get(path.split("/")[2], [])
            elif path.endswith("/directReports"):
                manager_id = path.split("/")[2]
                value = [{"id": identifier} for identifier, item in self.users.items() if item.get("manager_id") == manager_id]
            elif path.endswith("/members"):
                value = [{"id": identifier} for identifier in self.members[path.split("/")[2]]]
            else:
                raise AssertionError("Unexpected GET: " + path)
            return 200, {"Content-Type": "application/json"}, json.dumps({"value": value})
        if request.method == "POST" and path == "/users":
            assert not any(item["userPrincipalName"] == body["userPrincipalName"] for item in self.users.values())
            self.counter += 1
            identifier = str(UUID(int=self.counter))
            self.passwords.append(body["passwordProfile"]["password"])
            self.posted_profiles.append(body["passwordProfile"])
            attrs = {key: value for key, value in body.items() if key not in {"passwordProfile", "userPrincipalName"}}
            self.users[identifier] = user(identifier, body["userPrincipalName"], **attrs)
            return 201, {"Content-Type": "application/json"}, json.dumps({"id": identifier})
        if request.method == "POST" and path.endswith("/members/$ref"):
            group_id, identifier = path.split("/")[2], body["@odata.id"].rsplit("/", 1)[1]
            if identifier in self.members[group_id]:
                return 400, {}, '{"error":"duplicate membership"}'
            self.members[group_id].add(identifier)
            return 204, {}, ""
        if request.method == "PUT" and path.endswith("/manager/$ref"):
            self.users[path.split("/")[2]]["manager_id"] = body["@odata.id"].rsplit("/", 1)[1]
            return 204, {}, ""
        raise AssertionError("Unexpected request: " + request.method + path)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    config = Config(TENANT, "7181e33e-c973-4e48-8f0d-2b8ab40d5728", "fake-client-private", DOMAIN)
    state = TenantMock(config)
    folder = tmp_path / "private-state"
    def journal_factory(cfg):
        return Journal(cfg, folder=folder, protector=FakeProtector())
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "Journal", journal_factory)
    monkeypatch.setattr(cli, "GraphClient", lambda cfg: GraphClient(cfg, app=state.app, sleep=state.sleep))
    monkeypatch.setattr(cli, "GraphWriter", lambda cfg: GraphWriter(cfg, app=state.app, sleep=state.sleep))
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        for method in (responses.GET, responses.POST, responses.PUT):
            http.add_callback(method, re.compile(re.escape(BASE) + r"/.*"), callback=state.callback)
        yield state, http, journal_factory(config)


def run(mode, capsys):
    code = cli.main(["joiner", mode, "--csv", str(FIXTURES / "hr_feed.csv"),
                     "--group-map", str(FIXTURES / "group_map.json")])
    output = capsys.readouterr().out
    return code, [json.loads(line) for line in output.splitlines()], output


def writes(http):
    return [call for call in http.calls if call.request.method != "GET"]


def test_apply_creates_two_users_and_verifies_access_without_logging_passwords(lab, capsys):
    state, http, journal = lab
    code, events, output = run("--apply", capsys)
    summary = events[-1]
    assert code == 0 and summary["outcome"] == "success"
    assert {key: summary[key] for key in ("created_users", "added_memberships", "assigned_managers", "verified_users", "directory_writes")} == {
        "created_users": 2, "added_memberships": 4, "assigned_managers": 2, "verified_users": 2, "directory_writes": 8}
    assert len(writes(http)) == 8
    assert len(state.users) == 4
    assert state.passwords[0] != state.passwords[1]
    assert all(profile["forceChangePasswordNextSignIn"] is True for profile in state.posted_profiles)
    assert all(password not in output for password in state.passwords)
    assert "mock-token-private" not in output and "fake-client-private" not in output
    journal.load()
    assert journal.get("ILAB-001")["status"] == journal.get("ILAB-002")["status"] == "complete"
    assert all(password.encode() not in journal.path.read_bytes() for password in state.passwords)


def test_second_apply_is_noop_and_keeps_original_passwords_and_ids(lab, capsys):
    state, http, journal = lab
    assert run("--apply", capsys)[0] == 0
    before, passwords, number = deepcopy(state.users), list(state.passwords), len(writes(http))
    code, events, _ = run("--apply", capsys)
    assert code == 0 and events[-1]["unchanged_users"] == 2 and events[-1]["verified_users"] == 2
    assert events[-1]["directory_writes"] == 0 and events[-1]["created_users"] == 0
    assert state.users == before and state.passwords == passwords and len(writes(http)) == number


def test_dry_run_makes_only_gets_and_creates_no_state_folder(lab, capsys):
    state, http, journal = lab
    code, events, _ = run("--dry-run", capsys)
    assert code == 0 and events[-1]["actions"] == {"create_user": 2, "add_group_membership": 4, "set_manager": 2, "no_change": 0}
    assert not writes(http) and not journal.folder.exists() and not state.passwords


def test_verify_before_creation_fails_without_writes(lab, capsys):
    _, http, journal = lab
    code, events, _ = run("--verify", capsys)
    assert code == 1 and events[-1]["verified_users"] == 0 and events[-1]["directory_writes"] == 0
    assert not writes(http) and not journal.folder.exists()


def test_verify_and_second_dry_run_after_apply_are_read_only(lab, capsys):
    state, http, journal = lab
    assert run("--apply", capsys)[0] == 0
    before, count = journal.path.read_bytes(), len(writes(http))
    code, events, _ = run("--verify", capsys)
    assert code == 0 and events[-1]["verified_users"] == 2
    code, events, _ = run("--dry-run", capsys)
    assert code == 0 and events[-1]["actions"] == {"create_user": 0, "add_group_membership": 0, "set_manager": 0, "no_change": 2}
    assert len(writes(http)) == count and journal.path.read_bytes() == before


def test_partial_group_failure_resumes_without_duplicate_user_or_password(lab, capsys):
    state, http, journal = lab
    def denied(request, path, body):
        if request.method == "POST" and path.endswith("/members/$ref"):
            return 403, {}, '{"error":"private raw response"}'
    state.fail = denied
    code, events, output = run("--apply", capsys)
    assert code == 1 and "private raw response" not in output
    assert len(state.passwords) == 1 and journal.load().get("ILAB-001")["status"] == "created"
    first_password = state.passwords[0]
    state.fail = None
    code, events, _ = run("--apply", capsys)
    assert code == 0 and events[-1]["created_users"] == 1 and events[-1]["verified_users"] == 2
    assert len(state.passwords) == 2 and state.passwords[0] == first_password


def test_missing_group_and_changed_manager_are_repaired_without_password_reset(lab, capsys):
    state, http, journal = lab
    assert run("--apply", capsys)[0] == 0
    identifier = journal.load().get("ILAB-001")["user_id"]
    finance_group = next(key for key, value in state.groups.items() if value["displayName"].endswith("Finance"))
    state.members[finance_group].remove(identifier)
    state.users[identifier]["manager_id"] = IT
    passwords = list(state.passwords)
    code, events, _ = run("--apply", capsys)
    assert code == 0 and events[-1]["directory_writes"] == 2 and events[-1]["created_users"] == 0
    assert events[-1]["added_memberships"] == 1 and events[-1]["assigned_managers"] == 1
    assert state.passwords == passwords


@pytest.mark.parametrize("change", [{"accountEnabled": False}, {"department": "IT"}, {"employeeId": "OTHER-001"}, {"onPremisesSyncEnabled": True}])
def test_changed_or_disabled_account_blocks_batch_before_more_writes(lab, capsys, change):
    state, http, journal = lab
    assert run("--apply", capsys)[0] == 0
    identifier = journal.load().get("ILAB-001")["user_id"]
    state.users[identifier].update(change)
    count = len(writes(http))
    assert run("--apply", capsys)[0] == 1
    assert len(writes(http)) == count


def test_active_admin_assignment_blocks_rerun(lab, capsys):
    state, http, journal = lab
    assert run("--apply", capsys)[0] == 0
    identifier = journal.load().get("ILAB-001")["user_id"]
    state.roles[identifier] = [{"@odata.type": "#microsoft.graph.directoryRole", "id": str(UUID(int=900))}]
    count = len(writes(http))
    assert run("--apply", capsys)[0] == 1 and len(writes(http)) == count


def test_duplicate_second_employee_aborts_before_first_creation(lab, capsys):
    state, http, _ = lab
    for index in (800, 801):
        state.users[str(UUID(int=index))] = user(str(UUID(int=index)), f"dup{index}@{DOMAIN}", employeeId="ILAB-002")
    assert run("--apply", capsys)[0] == 1 and not writes(http)


def test_untracked_existing_user_is_not_adopted(lab, capsys):
    state, http, journal = lab
    assert run("--apply", capsys)[0] == 0
    journal.path.unlink()  # Simulate lost journal, not a recommended recovery step.
    count = len(writes(http))
    code, events, _ = run("--apply", capsys)
    assert code == 1 and "not owned" in events[-1]["message"] and len(writes(http)) == count


def test_creation_timeout_keeps_intent_and_blocks_blind_post_retry(lab, capsys):
    state, http, journal = lab
    def timeout(request, path, body):
        if request.method == "POST" and path == "/users":
            # Server commits, but the client never receives the 201 response.
            state.fail = None
            state.callback(request)
            raise requests.Timeout("private-token-and-password")
    state.fail = timeout
    code, events, output = run("--apply", capsys)
    assert code == 1 and "private-token-and-password" not in output
    assert len(state.users) == 3
    assert journal.load().get("ILAB-001")["status"] == "creating"
    count = len(writes(http))
    state.fail = None
    code, events, _ = run("--apply", capsys)
    assert code == 1 and "unknown outcome" in events[-1]["message"] and len(writes(http)) == count


def test_confirmed_creation_rejection_can_be_retried_after_fix(lab, capsys):
    state, http, journal = lab
    state.fail = lambda request, path, body: (403, {}, "private error") if request.method == "POST" and path == "/users" else None
    assert run("--apply", capsys)[0] == 1
    assert journal.load().get("ILAB-001") is None
    state.fail = None
    assert run("--apply", capsys)[0] == 0


def test_post_throttling_obeys_retry_after_and_creates_only_once(lab, capsys):
    state, http, journal = lab
    hits = []
    def throttled(request, path, body):
        if request.method == "POST" and path == "/users" and not hits:
            hits.append(True)
            return 429, {"Retry-After": "3"}, ""
    state.fail = throttled
    code, events, _ = run("--apply", capsys)
    assert code == 0 and events[-1]["directory_writes"] == 8 and events[-1]["write_attempts"] == 9
    state.sleep.assert_any_call(3.0)
    assert len(state.passwords) == 2


def test_membership_race_is_verified_before_treating_400_as_success(lab, capsys):
    state, http, journal = lab
    happened = []
    def raced(request, path, body):
        if request.method == "POST" and path.endswith("/members/$ref") and not happened:
            happened.append(True)
            state.members[path.split("/")[2]].add(body["@odata.id"].rsplit("/", 1)[1])
            return 400, {}, '{"error":"already exists"}'
    state.fail = raced
    code, events, _ = run("--apply", capsys)
    assert code == 0 and events[-1]["verified_users"] == 2
    assert any(event.get("outcome") == "already_present" for event in events)


def test_password_protection_failure_occurs_before_any_graph_write(lab, monkeypatch, capsys):
    state, http, journal = lab
    def broken(self, data):
        raise StateError("Test storage failure")
    monkeypatch.setattr(FakeProtector, "protect", broken)
    # Keep the test double's self-check simple; the failure is during intent save.
    code, events, _ = run("--apply", capsys)
    assert code == 1 and not writes(http) and not state.passwords


def test_user_500_response_not_retried(lab, capsys):
    state, http, journal = lab
    state.fail = lambda request, path, body: (500, {}, "private error") if request.method == "POST" and path == "/users" else None
    assert run("--apply", capsys)[0] == 1
    assert len(writes(http)) == 1 and journal.load().get("ILAB-001")["status"] == "creating"


def test_deterministic_collision_uses_suffix_two(lab, capsys):
    state, http, _ = lab
    state.users[str(UUID(int=899))] = user(str(UUID(int=899)), "riya.shah@" + DOMAIN)
    assert run("--apply", capsys)[0] == 0
    riya = next(item for item in state.users.values() if item.get("employeeId") == "ILAB-001")
    assert riya["userPrincipalName"] == "riya.shah2@" + DOMAIN


def test_existing_identity_disappearing_is_not_recreated(lab, capsys):
    state, http, journal = lab
    assert run("--apply", capsys)[0] == 0
    del state.users[journal.load().get("ILAB-001")["user_id"]]
    count = len(writes(http))
    assert run("--apply", capsys)[0] == 1 and len(writes(http)) == count


def test_future_start_is_deferred_and_terminated_feed_rejected(lab):
    state, _, _ = lab
    mapping = load_group_map(FIXTURES / "group_map.json")
    rows = load_feed(FIXTURES / "hr_feed.csv", mapping)
    with GraphClient(state.config, app=state.app) as client:
        directory = JoinerDirectory(client, state.config)
        items, deferred = prepare([replace(rows[0], start_date=date(2026, 10, 17))], mapping, directory, TODAY)
        assert not items and deferred[0]["outcome"] == "deferred"
        with pytest.raises(PlanError, match="terminated"):
            prepare([replace(rows[0], status="terminated", end_date=TODAY)], mapping, directory, TODAY)


@pytest.mark.parametrize("args", [["joiner"], ["joiner", "--dry-run", "--apply"], ["joiner", "--apply", "--as-of", "2026-10-16"]])
def test_cli_rejects_ambiguous_modes_or_apply_date_override(args):
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2


@pytest.mark.parametrize("method,path", [("DELETE", "/users/00000000-0000-0000-0000-000000000001"), ("PATCH", "/users"), ("POST", "https://untrusted.example/users")])
def test_write_transport_rejects_unimplemented_operations(lab, method, path):
    state, http, _ = lab
    with GraphWriter(state.config, app=state.app) as client:
        with pytest.raises(WriteError, match="outside"):
            client.write(method, path, {})
    assert not http.calls


def test_generated_password_meets_complexity():
    passwords = [new_password() for _ in range(10)]
    assert len(set(passwords)) == 10
    for password in passwords:
        assert len(password) == 32 and any(char.isupper() for char in password) and any(char.islower() for char in password)
        assert any(char.isdigit() for char in password) and any(char in "!@#%*-_+" for char in password)


def test_journal_atomic_save_locking_and_no_plaintext(tmp_path):
    config = Config(TENANT, str(UUID(int=22)), "fake", DOMAIN)
    first = Journal(config, folder=tmp_path, protector=FakeProtector())
    second = Journal(config, folder=tmp_path, protector=FakeProtector())
    with first:
        first.put("ILAB-001", {"password": "fake-user-secret", "status": "creating"})
        assert b"fake-user-secret" not in first.path.read_bytes()
        with pytest.raises(StateError, match="lock"):
            second.__enter__()
    assert not first.lock_path.exists()
    assert second.load().get("ilab-001")["status"] == "creating"
    with pytest.raises(StateError, match="lock"):
        second.save()


def test_journal_refuses_other_app_and_corrupt_data(tmp_path):
    config = Config(TENANT, str(UUID(int=22)), "fake", DOMAIN)
    with Journal(config, folder=tmp_path, protector=FakeProtector()) as journal:
        journal.save()
    different = Journal(replace(config, client_id=str(UUID(int=23))), folder=tmp_path, protector=FakeProtector())
    with pytest.raises(StateError, match="does not match"):
        different.load()
    journal.path.write_bytes(FakeProtector().protect(b"not JSON"))
    with pytest.raises(StateError, match="cannot be read"):
        journal.load()


def test_password_command_does_not_reveal_under_redirection(lab, capsys):
    code = cli.main(["password", "--employee-id", "ILAB-001"])
    assert code == 1 and "interactive PowerShell" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="Real DPAPI requires Windows; runs on the user's Windows machine.")
def test_windows_dpapi_real_round_trip():
    protector = WindowsProtector()
    protector.self_check()
    protected = protector.protect(b"test-only-secret")
    assert protected != b"test-only-secret"
    assert protector.unprotect(protected) == b"test-only-secret"
