"""Run with: python -m src.check_connection"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import ConfigError, load_config
from .graph_client import GraphClient, GraphError


def main():
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "connection_check",
        "actor": None,
        "target": None,
        "read_only": True,
    }
    try:
        config = load_config()
        event.update(actor=config.client_id, target=config.tenant_id)
        with GraphClient(config) as client:
            organizations = client.get_all(
                "/organization", params={"$select": "id,displayName,verifiedDomains"}
            )
        if len(organizations) != 1 or organizations[0].get("id") != config.tenant_id:
            raise GraphError("The organization response did not match the configured tenant ID.")
        organization = organizations[0]
        domains = {
            domain.get("name", "").lower()
            for domain in organization.get("verifiedDomains", [])
            if isinstance(domain, dict)
        }
        if config.upn_domain not in domains:
            raise GraphError("ENTRA_UPN_DOMAIN was not found in the tenant's verified domains.")
        event.update(
            outcome="success",
            tenant_name=organization.get("displayName"),
            expected_tenant=True,
            upn_domain=config.upn_domain,
            domain_verified=True,
        )
    except (ConfigError, GraphError) as error:
        event.update(outcome="failure", message=str(error))
        print(json.dumps(event))
        return 1
    print(json.dumps(event))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
