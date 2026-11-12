"""HTTP-mocked leaver integration tests; never contact a live tenant."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import urlsplit
from uuid import UUID

import pytest
import requests
import responses

from test_joiner_apply import (FakeProtector, Config, GraphClient, GraphWriter, Journal,
                               FIXTURES, BASE, DOMAIN, TENANT, IT, writes)
from test_mover import MoverTenant, MoverWriter, save_rows
from src import cli, mover_cli, leaver_cli
from src.graph_writer import WriteError
from src.leaver_writer import LeaverWriter


class LeaverTenant(MoverTenant):
    def __init__(self, config):
        super().__init__(config)
        self.delay_revocation = False
        self.delay_licenses = False

    def advance_stamp(self, identifier):
        value = self.users[identifier]["signInSessionsValidFromDateTime"]
        before = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.users[identifier]["signInSessionsValidFromDateTime"] = (before + timedelta(minutes=1)).isoformat()

    def refresh_licenses(self, identifier):
        account = self.users[identifier]
        account["assignedLicenses"] = [{"skuId": sku, "disabledPlans": []}
                                       for sku in sorted({s["skuId"] for s in account["licenseAssignmentStates"]})]

    def callback(self, request):
        path = urlsplit(request.url).path[len("/v1.0"):]
        body = json.loads(request.body) if request.body else None
        custom = ((request.method == "PATCH" and set(body or {}) == {"accountEnabled"})
                  or path.endswith("/revokeSignInSessions") or path.endswith("/assignLicense")
                  or (request.method == "GET" and path.endswith("/transitiveMemberOf"))
                  or request.method == "DELETE")
        if not custom:
            return super().callback(request)
        if self.fail:
            override = self.fail(request, path, body)
            if override is not None:
                return override
        if request.method == "GET":
            identifier = path.split("/")[2]
            value = deepcopy(self.roles.get(identifier, [])) + [
                {"id": group_id, "@odata.type": "#microsoft.graph.group"}
                for group_id, members in self.members.items() if identifier in members]
            return 200, {"Content-Type": "application/json"}, json.dumps({"value": value})
        if request.method == "PATCH":
            assert body["accountEnabled"] is False
            self.users[path.split("/")[2]].update(body)
            return 204, {}, ""
        if path.endswith("/revokeSignInSessions"):
            assert request.method == "POST" and request.body is None
            if not self.delay_revocation:
                self.advance_stamp(path.split("/")[2])
            return 200, {"Content-Type": "application/json"}, '{"value":true}'
        if path.endswith("/assignLicense"):
            assert request.method == "POST" and body["addLicenses"] == []
            identifier = path.split("/")[2]
            if not self.delay_licenses:
                self.users[identifier]["licenseAssignmentStates"] = [
                    s for s in self.users[identifier]["licenseAssignmentStates"]
                    if not (s["assignedByGroup"] is None and s["skuId"] in body["removeLicenses"])]
                self.refresh_licenses(identifier)
            return 200, {"Content-Type": "application/json"}, json.dumps(self.users[identifier])
        assert request.method == "DELETE" and path.endswith("/$ref") and request.body is None
        group_id, identifier = path.split("/")[2], path.split("/")[4]
        if identifier not in self.members[group_id]:
            return 404, {}, "already absent"
        self.members[group_id].remove(identifier)
        if not self.delay_licenses and "licenseAssignmentStates" in self.users[identifier]:
            self.users[identifier]["licenseAssignmentStates"] = [
                s for s in self.users[identifier]["licenseAssignmentStates"] if s["assignedByGroup"] != group_id]
            self.refresh_licenses(identifier)
        return 204, {}, ""


@pytest.fixture
def lab(tmp_path, monkeypatch, capsys):
    import csv
    config = Config(TENANT, "7181e33e-c973-4e48-8f0d-2b8ab40d5728", "fake-client-private", DOMAIN)
    state = LeaverTenant(config)
    folder = tmp_path / "private-state"
    def journal_factory(cfg):
        return Journal(cfg, folder=folder, protector=FakeProtector())
    for module in (cli, mover_cli, leaver_cli):
        monkeypatch.setattr(module, "load_config", lambda: config)
        monkeypatch.setattr(module, "Journal", journal_factory)
        monkeypatch.setattr(module, "GraphClient", lambda cfg: GraphClient(cfg, app=state.app, sleep=state.sleep))
    monkeypatch.setattr(cli, "GraphWriter", lambda cfg: GraphWriter(cfg, app=state.app, sleep=state.sleep))
    monkeypatch.setattr(mover_cli, "MoverWriter", lambda cfg: MoverWriter(cfg, app=state.app, sleep=state.sleep))
    monkeypatch.setattr(leaver_cli, "LeaverWriter", lambda cfg: LeaverWriter(cfg, app=state.app, sleep=state.sleep))
    state.journal_factory = journal_factory
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        for method in (responses.GET, responses.POST, responses.PUT, responses.DELETE, responses.PATCH):
            http.add_callback(method, re.compile(re.escape(BASE) + r"/.*"), callback=state.callback)
        common = ["--group-map", str(FIXTURES / "group_map.json")]
        assert cli.main(["joiner", "--apply", "--csv", str(FIXTURES / "hr_feed.csv"), *common]) == 0
        with (FIXTURES / "hr_feed.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        rows[0].update(department="IT", job_title="IT Analyst", manager_upn="it.manager@" + DOMAIN)
        feed = tmp_path / "hr_feed_leaver.csv"
        save_rows(feed, rows)
        assert cli.main(["mover", "--apply", "--csv", str(feed), *common]) == 0
        journal = journal_factory(config).load()
        state.riya, state.arjun = (journal.get(key)["user_id"] for key in ("ILAB-001", "ILAB-002"))
        state.group_id = lambda suffix: next(key for key, value in state.groups.items() if value["displayName"].endswith(suffix))
        for account in state.users.values():
            account.update(assignedLicenses=[], licenseAssignmentStates=[],
                           signInSessionsValidFromDateTime="2026-10-01T00:00:00Z")
        rows[0].update(status="terminated", end_date=datetime.now(timezone.utc).date().isoformat())
        save_rows(feed, rows)
        capsys.readouterr()
        yield state, http, journal, feed, rows


def run(lab, capsys, mode, *extra):
    args = ["leaver", mode, "--csv", str(lab[3]), "--group-map", str(FIXTURES / "group_map.json")]
    if mode == "--apply":
        args += ["--log-file", str(lab[3].parent / "leaver.jsonl")]
    code = cli.main([*args, *extra])
    output = capsys.readouterr().out
    return code, [json.loads(line) for line in output.splitlines()], output


def add_license(state, source=None, sku=None):
    sku = sku or str(UUID(int=5001))
    state.users[state.riya]["licenseAssignmentStates"].append({
        "skuId": sku, "assignedByGroup": source, "state": "Active", "error": None})
    state.refresh_licenses(state.riya)
    return sku


def test_dry_run_four_changes_and_no_state_or_directory_writes(lab, capsys):
    state, http, journal, _, _ = lab
    before, data, count = deepcopy(state.users), journal.path.read_bytes(), len(writes(http))
    code, events, _ = run(lab, capsys, "--dry-run")
    assert code == 0
    assert events[-1]["actions"] == {"disable_account": 1, "revoke_sessions": 1,
        "remove_group_membership": 2, "remove_direct_licenses": 0, "reassign_direct_report": 0}
    assert events[-1]["planned_directory_writes"] == 4 and events[-1]["directory_writes"] == 0
    assert events[-1]["active_controls"] == 1
    assert not (lab[3].parent / "leaver.jsonl").exists()
    assert journal.path.read_bytes() == data and not journal.lock_path.exists()
    assert state.users == before and len(writes(http)) == count


def test_apply_disables_revokes_removes_and_preserves_arjun_and_passwords(lab, capsys):
    state, http, journal, feed, _ = lab
    arjun, passwords, count = deepcopy(state.users[state.arjun]), list(state.passwords), len(writes(http))
    ids = set(state.users)
    code, events, output = run(lab, capsys, "--apply")
    assert code == 0
    summary = events[-1]
    assert {key: summary[key] for key in ("disabled_accounts", "revocation_requests", "removed_memberships",
        "verified_leavers", "active_controls_verified", "directory_writes", "write_attempts")} == {
        "disabled_accounts": 1, "revocation_requests": 1, "removed_memberships": 2,
        "verified_leavers": 1, "active_controls_verified": 1, "directory_writes": 4, "write_attempts": 4}
    actual = writes(http)[count:]
    assert [call.request.method for call in actual] == ["PATCH", "POST", "DELETE", "DELETE"]
    assert all(call.request.url.endswith("/$ref") for call in actual if call.request.method == "DELETE")
    assert state.users[state.riya]["accountEnabled"] is False
    assert state.users[state.riya]["manager_id"] == IT
    assert all(state.riya not in members for members in state.members.values())
    assert state.users[state.arjun] == arjun and set(state.users) == ids and state.passwords == passwords
    removed = [e for e in events if e.get("action") == "remove_group_membership" and e["outcome"] == "applied"]
    assert len(removed) == 2
    assert all(e["timestamp"].endswith("+00:00") and e["actor"] == state.config.client_id
               and e["target"] == "ILAB-001" and e["http_status"] == 204 for e in removed)
    assert next(e for e in events if e["event"] == "leaver_verified")["license_removal_test"] == "NOT_TESTED_NO_ASSIGNMENT"
    assert all(secret not in output for secret in passwords + ["mock-token-private", "fake-client-private"])
    assert (feed.parent / "leaver.jsonl").read_text() == output
    assert journal.load().get("ILAB-001")["status"] == "left"
    assert journal.get("ILAB-002")["status"] == "complete"


def test_verify_before_apply_is_read_only_and_fails(lab, capsys):
    _, http, journal, _, _ = lab
    before, count = journal.path.read_bytes(), len(writes(http))
    code, events, _ = run(lab, capsys, "--verify")
    assert code == 1 and events[-1]["verified_leavers"] == 0 and events[-1]["directory_writes"] == 0
    assert journal.path.read_bytes() == before and len(writes(http)) == count


def test_verify_and_safe_rerun_leave_journal_and_directory_unchanged(lab, capsys):
    state, http, journal, _, _ = lab
    assert run(lab, capsys, "--apply")[0] == 0
    before, data, count = deepcopy(state.users), journal.path.read_bytes(), len(writes(http))
    assert run(lab, capsys, "--verify")[0] == 0
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["unchanged_leavers"] == 1
    assert events[-1]["directory_writes"] == events[-1]["write_attempts"] == 0
    assert state.users == before and journal.path.read_bytes() == data and len(writes(http)) == count
    code, events, _ = run(lab, capsys, "--dry-run")
    assert code == 0 and events[-1]["planned_directory_writes"] == 0


@pytest.mark.parametrize("source", ["direct", "group", "both"])
def test_license_sources_removed_through_correct_operations(lab, capsys, source):
    state, http, _, _, _ = lab
    sku = str(UUID(int=5001))
    if source in {"direct", "both"}:
        add_license(state, sku=sku)
    if source in {"group", "both"}:
        add_license(state, state.group_id("IT"), sku=sku)
    count = len(writes(http))
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0
    license_calls = [c for c in writes(http)[count:] if c.request.url.endswith("/assignLicense")]
    assert len(license_calls) == (source != "group")
    if license_calls:
        assert json.loads(license_calls[0].request.body) == {"addLicenses": [], "removeLicenses": [sku]}
    assert state.users[state.riya]["assignedLicenses"] == []
    assert state.users[state.riya]["licenseAssignmentStates"] == []
    assert next(e for e in events if e["event"] == "leaver_verified")["license_removal_test"] == "CLEANUP_VERIFIED"


def test_inherited_license_propagation_is_pending_then_resumes_without_repeating_writes(lab, capsys):
    state, http, _, _, _ = lab
    add_license(state, state.group_id("IT"))
    state.delay_licenses = True
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 1 and "verification is pending" in events[-1]["message"]
    assert state.users[state.riya]["accountEnabled"] is False
    state.users[state.riya]["assignedLicenses"] = []
    state.users[state.riya]["licenseAssignmentStates"] = []
    count = len(writes(http))
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["directory_writes"] == 0 and len(writes(http)) == count


def test_accepted_direct_license_removal_is_not_duplicated_during_propagation(lab, capsys):
    state, http, _, _, _ = lab
    add_license(state)
    state.delay_licenses = True
    assert run(lab, capsys, "--apply")[0] == 1
    count = len(writes(http))
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 1 and "No duplicate write" in events[-1]["message"]
    assert len(writes(http)) == count
    state.users[state.riya]["assignedLicenses"] = []
    state.users[state.riya]["licenseAssignmentStates"] = []
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["directory_writes"] == 0


def test_completed_leaver_is_not_reenabled_by_old_joiner_or_mover_feed(lab, capsys):
    state, http, _, feed, rows = lab
    assert run(lab, capsys, "--apply")[0] == 0
    count = len(writes(http))
    rows[0].update(status="active", end_date="")
    save_rows(feed, rows)
    for command in ("joiner", "mover"):
        assert cli.main([command, "--apply", "--csv", str(feed),
                         "--group-map", str(FIXTURES / "group_map.json")]) == 1
        capsys.readouterr()
    assert len(writes(http)) == count and state.users[state.riya]["accountEnabled"] is False


@pytest.mark.parametrize("operation", ["disable", "revoke", "membership", "license"])
def test_failure_stops_then_resumes_the_remaining_work(lab, capsys, operation):
    state, http, _, _, _ = lab
    add_license(state)
    def denied(req, path, body):
        match = ((operation == "disable" and req.method == "PATCH")
                 or (operation == "revoke" and path.endswith("/revokeSignInSessions"))
                 or (operation == "membership" and req.method == "DELETE")
                 or (operation == "license" and path.endswith("/assignLicense")))
        return (403, {}, "private response") if match else None
    state.fail = denied
    code, events, output = run(lab, capsys, "--apply")
    assert code == 1 and events[-1]["outcome"] == "failure" and "private response" not in output
    assert state.users[state.arjun]["accountEnabled"] is True
    state.fail = None
    assert run(lab, capsys, "--apply")[0] == 0
    assert run(lab, capsys, "--verify")[0] == 0
    successful_revokes = [c for c in writes(http) if c.request.url.endswith("/revokeSignInSessions") and c.response.status_code == 200]
    assert len(successful_revokes) == 1


def test_timeout_after_group_removal_is_recovered_and_audited(lab, capsys):
    state, http, _, _, _ = lab
    def timeout(req, path, body):
        if req.method == "DELETE":
            state.members[path.split("/")[2]].remove(state.riya)
            raise requests.Timeout()
    state.fail = timeout
    assert run(lab, capsys, "--apply")[0] == 1
    state.fail = None
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["removed_memberships"] == 1
    recovered = next(e for e in events if e["event"] == "leaver_recovery")
    assert recovered["action"] == "remove_group_membership"
    assert recovered["outcome"] == "desired_state_observed_on_resume" and "http_status" not in recovered
    deletes = [c for c in writes(http) if c.request.method == "DELETE"]
    assert len({c.request.url for c in deletes}) == len(deletes)


@pytest.mark.parametrize("committed", [True, False])
def test_unknown_revocation_requires_read_back_or_explicit_retry(lab, capsys, committed):
    state, http, _, _, _ = lab
    def timeout(req, path, body):
        if path.endswith("/revokeSignInSessions"):
            if committed:
                state.advance_stamp(state.riya)
            raise requests.Timeout()
    state.fail = timeout
    assert run(lab, capsys, "--apply")[0] == 1
    state.fail = None
    code, events, _ = run(lab, capsys, "--apply")
    if committed:
        assert code == 0
    else:
        assert code == 1 and "unconfirmed" in events[-1]["message"]
        count = len(writes(http))
        code, events, _ = run(lab, capsys, "--dry-run")
        assert code == 0 and events[-1]["pending_verifications"] == 1 and len(writes(http)) == count
        assert run(lab, capsys, "--apply", "--retry-unconfirmed-revocation")[0] == 0
    revokes = [c for c in writes(http) if c.request.url.endswith("/revokeSignInSessions")]
    assert len(revokes) == (1 if committed else 2)


def test_accepted_revocation_waits_for_timestamp_without_reposting(lab, capsys):
    state, http, _, _, _ = lab
    state.delay_revocation = True
    assert run(lab, capsys, "--apply")[0] == 1
    assert state.users[state.riya]["accountEnabled"] is False
    assert all(state.riya not in members for members in state.members.values())
    assert run(lab, capsys, "--apply")[0] == 1
    state.advance_stamp(state.riya)
    assert run(lab, capsys, "--apply")[0] == 0
    assert len([c for c in writes(http) if c.request.url.endswith("/revokeSignInSessions")]) == 1


@pytest.mark.parametrize("status,payload,success", [(204, "", True), (200, '{"value":false}', False)])
def test_revocation_accepts_documented_2xx_but_never_a_false_result(lab, capsys, status, payload, success):
    state = lab[0]
    def response(req, path, body):
        if path.endswith("/revokeSignInSessions"):
            if success:
                state.advance_stamp(state.riya)
            return status, {"Content-Type": "application/json"}, payload
    state.fail = response
    code, events, _ = run(lab, capsys, "--apply")
    assert code == (0 if success else 1)
    if success:
        revoke_event = next(e for e in events if e.get("action") == "revoke_sessions" and e["outcome"] == "applied")
        assert revoke_event["http_status"] == 204 and revoke_event["response_value"] is None
    else:
        assert "confirm" in events[-1]["message"] and state.riya in state.members[state.group_id("IT")]


def test_already_disabled_user_still_gets_revocation_and_cleanup(lab, capsys):
    state = lab[0]
    state.users[state.riya]["accountEnabled"] = False
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["disabled_accounts"] == 0
    assert events[-1]["revocation_requests"] == 1 and events[-1]["removed_memberships"] == 2


def test_reenabled_completed_leaver_is_disabled_and_revoked_again(lab, capsys):
    state = lab[0]
    assert run(lab, capsys, "--apply")[0] == 0
    state.users[state.riya]["accountEnabled"] = True
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["disabled_accounts"] == events[-1]["revocation_requests"] == 1
    assert events[-1]["directory_writes"] == 2


def test_access_drift_is_removed_without_repeating_revocation(lab, capsys):
    state = lab[0]
    assert run(lab, capsys, "--apply")[0] == 0
    state.members[state.group_id("IT")].add(state.riya)
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["removed_memberships"] == 1 and events[-1]["revocation_requests"] == 0


@pytest.mark.parametrize("change", ["unmapped_group", "unmapped_license", "admin", "synchronized", "foreign_id", "unlicensed_unknown", "active_disabled"])
def test_scope_or_unknown_state_blocks_whole_batch_before_writes(lab, capsys, change):
    state, http, _, _, _ = lab
    outside = str(UUID(int=9001))
    if change == "unmapped_group":
        state.members[outside] = {state.riya}
    elif change == "unmapped_license":
        add_license(state, outside)
    elif change == "admin":
        state.roles[state.riya] = [{"id": outside, "@odata.type": "#microsoft.graph.directoryRole"}]
    elif change == "synchronized":
        state.users[state.riya]["onPremisesSyncEnabled"] = True
    elif change == "foreign_id":
        state.users[state.riya]["employeeId"] = "OTHER-001"
    elif change == "unlicensed_unknown":
        del state.users[state.riya]["licenseAssignmentStates"]
    elif change == "active_disabled":
        state.users[state.arjun]["accountEnabled"] = False
    count = len(writes(http))
    assert run(lab, capsys, "--apply")[0] == 1 and len(writes(http)) == count


def test_future_termination_and_absent_employee_never_offboard(lab, capsys):
    state, http, _, feed, rows = lab
    rows[0]["end_date"] = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    save_rows(feed, rows)
    count = len(writes(http))
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["eligible_leavers"] == 0 and events[-1]["deferred_rows"] == 1
    save_rows(feed, rows[1:])
    assert run(lab, capsys, "--apply")[0] == 0 and len(writes(http)) == count
    assert state.users[state.riya]["accountEnabled"] is True


def test_missing_or_unowned_user_is_never_adopted_or_created(lab, capsys):
    state, http, journal, _, _ = lab
    with state.journal_factory(state.config) as loaded:
        loaded.data["users"].pop("ilab-001")
        loaded.put("ILAB-002", loaded.get("ILAB-002"))
    count = len(writes(http))
    assert run(lab, capsys, "--apply")[0] == 1
    del state.users[state.riya]
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["skipped_rows"] == 1
    assert len(writes(http)) == count


def configure_report(lab):
    state, _, _, feed, rows = lab
    state.users[state.arjun]["manager_id"] = state.riya
    with state.journal_factory(state.config) as journal:
        record = journal.get("ILAB-002")
        record["identity"]["manager_id"] = state.riya
        journal.put("ILAB-002", record)
    rows[1]["manager_upn"] = state.users[state.riya]["userPrincipalName"]
    save_rows(feed, rows)
    path = feed.parent / "manager-replacements.json"
    path.write_text(json.dumps({"ILAB-001": "it.manager@" + DOMAIN}))
    return ["--manager-replacements", str(path)]


def test_direct_reports_require_explicit_plan_and_approved_reassignment_is_verified(lab, capsys):
    state, http, journal, _, _ = lab
    options = configure_report(lab)
    count = len(writes(http))
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 1 and "direct reports" in events[-1]["message"] and len(writes(http)) == count
    code, events, _ = run(lab, capsys, "--apply", *options)
    assert code == 0 and events[-1]["reassigned_reports"] == 1
    assert state.users[state.arjun]["manager_id"] == IT and state.users[state.arjun]["accountEnabled"] is True
    assert journal.load().get("ILAB-002")["identity"]["manager_id"] == IT
    assert run(lab, capsys, "--verify", *options)[0] == 0
    code, events, _ = run(lab, capsys, "--apply", *options)
    assert code == 0 and events[-1]["directory_writes"] == 0


def test_timeout_after_report_reassignment_recovers_journal_without_second_put(lab, capsys):
    state, http, journal, _, _ = lab
    options = configure_report(lab)
    def timeout(req, path, body):
        if req.method == "PUT":
            state.users[state.arjun]["manager_id"] = IT
            raise requests.Timeout()
    state.fail = timeout
    count = len(writes(http))
    assert run(lab, capsys, "--apply", *options)[0] == 1
    state.fail = None
    code, events, _ = run(lab, capsys, "--apply", *options)
    assert code == 0 and events[-1]["reassigned_reports"] == 0
    assert journal.load().get("ILAB-002")["identity"]["manager_id"] == IT
    assert len([c for c in writes(http)[count:] if c.request.method == "PUT"]) == 1


def test_throttling_respects_retry_after(lab, capsys):
    state = lab[0]
    seen = []
    def throttle(req, path, body):
        if req.method == "PATCH" and not seen:
            seen.append(True)
            return 429, {"Retry-After": "9"}, "throttled"
    state.fail = throttle
    code, events, _ = run(lab, capsys, "--apply")
    assert code == 0 and events[-1]["write_attempts"] == 5
    state.sleep.assert_any_call(9.0)


def test_server_failure_does_not_blindly_repeat_write(lab, capsys):
    state, http, _, _, _ = lab
    state.fail = lambda req, path, body: (500, {}, "private") if req.method == "PATCH" else None
    count = len(writes(http))
    assert run(lab, capsys, "--apply")[0] == 1 and len(writes(http)) == count + 1


@pytest.mark.parametrize("method,path,body", [
    ("DELETE", "/users/{user}", None),
    ("DELETE", "/groups/{group}/members/{user}", None),
    ("PATCH", "/users/{user}", {"accountEnabled": True}),
    ("PATCH", "/users/{user}", {"accountEnabled": 0}),
    ("POST", "/users", {}),
    ("POST", "/users/{user}/revokeSignInSessions", {}),
    ("POST", "/users/{user}/assignLicense", {"addLicenses": [], "removeLicenses": [str(UUID(int=9009))]})
])
def test_write_guard_rejects_unsafe_or_unplanned_requests(lab, method, path, body):
    state, http, _, _, _ = lab
    with LeaverWriter(state.config, app=state.app) as writer:
        writer.users = {state.riya}
        writer.groups = {state.group_id("IT")}
        writer.skus = {state.riya: set()}
        count = len(writes(http))
        with pytest.raises(WriteError, match="refused"):
            writer.write(method, path.format(user=state.riya, group=state.group_id("IT")), body)
        assert writer.write_attempts == 0 and len(writes(http)) == count


def test_invalid_csv_does_not_authenticate_or_call_graph(lab, capsys):
    state, http, _, feed, _ = lab
    feed.write_text("employee_id,first_name\nILAB-001,Riya\n")
    count, tokens = len(http.calls), state.app.acquire_token_for_client.call_count
    assert run(lab, capsys, "--apply")[0] == 1
    assert len(http.calls) == count and state.app.acquire_token_for_client.call_count == tokens


@pytest.mark.parametrize("extra", [[], ["--dry-run", "--log-file", "bad.jsonl"],
                                  ["--apply"], ["--verify", "--retry-unconfirmed-revocation"]])
def test_cli_rejects_incomplete_or_conflicting_modes(extra):
    with pytest.raises(SystemExit) as error:
        cli.main(["leaver", "--csv", "unused.csv", *extra])
    assert error.value.code == 2
