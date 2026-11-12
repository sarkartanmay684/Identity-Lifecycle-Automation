"""Load configuration without displaying the client secret."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from dotenv import dotenv_values


class ConfigError(ValueError):
    """A required setting is missing or invalid."""


@dataclass(frozen=True)
class Config:
    tenant_id: str
    client_id: str
    client_secret: str = field(repr=False)
    upn_domain: str


def load_config(env_file: str | Path | None = None) -> Config:
    """Environment variables take precedence over the project's .env file."""
    path = Path(env_file) if env_file is not None else Path(__file__).resolve().parents[1] / ".env"
    # Disable interpolation so characters in a secret are kept literally.
    file_values = dotenv_values(path, interpolate=False) if path.is_file() else {}
    names = (
        "ENTRA_TENANT_ID", "ENTRA_CLIENT_ID",
        "ENTRA_CLIENT_SECRET", "ENTRA_UPN_DOMAIN",
    )
    values = {name: os.environ.get(name, file_values.get(name)) for name in names}
    missing = [name for name, value in values.items() if not value or not value.strip()]
    if missing:
        raise ConfigError("Missing settings: " + ", ".join(missing))

    for name in ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID"):
        try:
            values[name] = str(UUID(values[name].strip()))
        except ValueError:
            raise ConfigError(f"{name} must contain a valid GUID.") from None

    domain = values["ENTRA_UPN_DOMAIN"].strip().lower()
    labels = domain.split(".")
    valid_labels = all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
    if len(labels) < 2 or len(domain) > 253 or not valid_labels:
        raise ConfigError("ENTRA_UPN_DOMAIN must be a domain name, for example hybrididentity.site.")

    return Config(
        tenant_id=values["ENTRA_TENANT_ID"],
        client_id=values["ENTRA_CLIENT_ID"],
        client_secret=values["ENTRA_CLIENT_SECRET"],
        upn_domain=domain,
    )
