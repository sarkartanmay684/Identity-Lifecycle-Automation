"""Offline acceptance tests for the reviewer quickstart."""
import json
import re
from urllib.parse import urlsplit

import pytest
import responses

from src import cli, preview_sample
from test_joiner_apply import BASE, TENANT, Config, FakeProtector, GraphClient, Journal, TenantMock


def test_quickstart_plans_in_another_domain_without_writes(tmp_path, monkeypatch, capsys):
    domain = "reviewer.example"
    cfg = Config(TENANT, "7181e33e-c973-4e48-8f0d-2b8ab40d5728", "mock-only", domain)
    for key, value in {"ENTRA_TENANT_ID": cfg.tenant_id, "ENTRA_CLIENT_ID": cfg.client_id,
                       "ENTRA_CLIENT_SECRET": cfg.client_secret, "ENTRA_UPN_DOMAIN": domain}.items():
        monkeypatch.setenv(key, value)
    tenant = TenantMock(cfg)
    for user in tenant.users.values():
        user["userPrincipalName"] = user["userPrincipalName"].split("@")[0] + "@" + domain
    def callback(req):
        if urlsplit(req.url).path == "/v1.0/organization":
            return 200, {"Content-Type": "application/json"}, json.dumps({"value": [
                {"id": cfg.tenant_id, "displayName": "Reviewer test", "verifiedDomains": [{"name": domain}]}
            ]})
        return tenant.callback(req)
    state = tmp_path / "private-state"
    monkeypatch.setattr(cli, "Journal", lambda config: Journal(config, folder=state, protector=FakeProtector()))
    monkeypatch.setattr(cli, "GraphClient",
                        lambda config: GraphClient(config, app=tenant.app, sleep=tenant.sleep))
    with responses.RequestsMock() as http:
        http.add_callback(responses.GET, re.compile(re.escape(BASE) + r"/.*"), callback=callback)
        assert preview_sample.main(["--dry-run"]) == 0
        assert http.calls and all(call.request.method == "GET" for call in http.calls)
    output = capsys.readouterr().out
    events = [json.loads(line) for line in output.splitlines()]
    assert events[-1]["directory_writes"] == 0
    assert events[-1]["actions"] == {"create_user": 1, "add_group_membership": 2,
                                     "set_manager": 1, "no_change": 0}
    create = next(e for e in events if e.get("action") == "create_user")
    assert create["attributes"]["userPrincipalName"] == "asha.preview@reviewer.example"
    assert "it.manager@reviewer.example" in output and cfg.client_secret not in output
    assert not state.exists()


def test_quickstart_cannot_apply(monkeypatch):
    monkeypatch.setattr(preview_sample, "load_config", lambda: pytest.fail("Unexpected config access"))
    with pytest.raises(SystemExit) as err:
        preview_sample.main(["--dry-run", "--apply"])
    assert err.value.code == 2
