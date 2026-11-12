"""Offline tests. No real tenant or credentials are used."""

import json
from unittest.mock import Mock

import pytest
import requests

from src import check_connection
from src.config import Config, ConfigError, load_config
from src.graph_client import GraphClient, GraphError


TENANT = "00000000-0000-0000-0000-000000000001"
CLIENT = "00000000-0000-0000-0000-000000000002"


@pytest.fixture
def config():
    return Config(TENANT, CLIENT, "TEST-ONLY-NOT-A-CREDENTIAL", "example.onmicrosoft.com")


@pytest.fixture
def client(config):
    app = Mock()
    app.acquire_token_for_client.return_value = {"access_token": "TEST-ONLY-TOKEN"}
    return GraphClient(config, app=app, session=Mock(), sleep=Mock())


def response(status, body=None, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = body or {}
    return result


def test_env_file_and_environment_precedence_without_secret_in_repr(tmp_path, monkeypatch):
    for key in ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET", "ENTRA_UPN_DOMAIN"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text(
        f"ENTRA_TENANT_ID={TENANT}\nENTRA_CLIENT_ID={CLIENT}\n"
        "ENTRA_CLIENT_SECRET=literal-${NOT_A_VARIABLE}\n"
        "ENTRA_UPN_DOMAIN=old.example.com\n", encoding="utf-8",
    )
    monkeypatch.setenv("ENTRA_UPN_DOMAIN", "EXAMPLE.ONMICROSOFT.COM")
    result = load_config(path)
    assert result.upn_domain == "example.onmicrosoft.com"
    assert result.client_secret == "literal-${NOT_A_VARIABLE}"
    assert result.client_secret not in repr(result)


def test_missing_settings_rejected_before_authentication(tmp_path, monkeypatch):
    for key in ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET", "ENTRA_UPN_DOMAIN"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ConfigError, match="Missing settings"):
        load_config(tmp_path / "absent.env")


def test_authentication_failure_sends_no_graph_request_or_secret(client):
    client.app.acquire_token_for_client.return_value = {
        "error": "invalid_client", "error_description": "TEST-ONLY-NOT-A-CREDENTIAL",
    }
    with pytest.raises(GraphError) as error:
        client.get("/organization")
    assert "invalid_client" in str(error.value)
    assert "TEST-ONLY-NOT-A-CREDENTIAL" not in str(error.value)
    client.session.get.assert_not_called()


def test_throttling_respects_retry_after_then_succeeds(client):
    client.session.get.side_effect = [
        response(429, headers={"Retry-After": "7"}),
        response(200, {"value": [{"id": TENANT}]}),
    ]
    assert client.get_all("/organization") == [{"id": TENANT}]
    client._sleep.assert_called_once_with(7.0)
    assert client.session.get.call_count == 2


def test_throttling_without_header_backs_off_and_stops(client):
    client.session.get.side_effect = [response(429) for _ in range(4)]
    with pytest.raises(GraphError, match="HTTP 429"):
        client.get("/organization")
    assert [call.args[0] for call in client._sleep.call_args_list] == [1, 2, 4]
    assert client.session.get.call_count == 4


def test_pagination_collects_every_page_without_reusing_first_page_params(client):
    next_link = "https://graph.microsoft.com/v1.0/users?$skiptoken=example"
    client.session.get.side_effect = [
        response(200, {"value": [{"id": "one"}], "@odata.nextLink": next_link}),
        response(200, {"value": [{"id": "two"}]}),
    ]
    assert client.get_all("/users", params={"$top": 1}) == [{"id": "one"}, {"id": "two"}]
    assert client.session.get.call_args_list[1].kwargs["params"] is None
    assert client.session.get.call_args_list[1].args[0] == next_link


def test_pagination_never_sends_token_to_a_different_host(client):
    client.session.get.return_value = response(
        200, {"value": [], "@odata.nextLink": "https://untrusted.example/users"}
    )
    with pytest.raises(GraphError, match="outside Microsoft Graph"):
        client.get_all("/users")
    assert client.session.get.call_count == 1


def test_forbidden_is_reported_without_retry_or_raw_response(client):
    client.session.get.return_value = response(403, {"error": "sensitive response content"})
    with pytest.raises(GraphError, match="HTTP 403") as error:
        client.get("/organization")
    assert "sensitive response content" not in str(error.value)
    assert client.session.get.call_count == 1
    client._sleep.assert_not_called()


def test_network_error_does_not_leak_request_details(client):
    client.session.get.side_effect = requests.ConnectionError("TEST-ONLY-TOKEN")
    with pytest.raises(GraphError) as error:
        client.get("/organization")
    assert "TEST-ONLY-TOKEN" not in str(error.value)


@pytest.mark.parametrize(
    "returned_id, domains, expected_status",
    [
        (TENANT, [{"name": "example.onmicrosoft.com"}], 0),
        (CLIENT, [{"name": "example.onmicrosoft.com"}], 1),
        (TENANT, [{"name": "different.example.com"}], 1),
    ],
)
def test_connection_check_requires_expected_tenant_and_domain(
    config, monkeypatch, capsys, returned_id, domains, expected_status
):
    fake_client = Mock()
    fake_client.get_all.return_value = [
        {"id": returned_id, "displayName": "Test Tenant", "verifiedDomains": domains}
    ]
    fake_context = Mock()
    fake_context.__enter__ = Mock(return_value=fake_client)
    fake_context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(check_connection, "load_config", lambda: config)
    monkeypatch.setattr(check_connection, "GraphClient", lambda _: fake_context)
    assert check_connection.main() == expected_status
    output = capsys.readouterr().out
    event = json.loads(output)
    assert event["read_only"] is True
    assert event["outcome"] == ("success" if expected_status == 0 else "failure")
    assert config.client_secret not in output
