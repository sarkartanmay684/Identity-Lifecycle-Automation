"""Read-only Graph client for the first Python checkpoint.

MSAL owns the application-token cache. Graph calls are GET-only here.
Mutation methods and lifecycle planning will be added in the next phase.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import msal
import requests

from .config import Config


class GraphError(RuntimeError):
    """An API failure described without credentials or token contents."""


class GraphClient:
    BASE_URL = "https://graph.microsoft.com/v1.0"
    SCOPES = ["https://graph.microsoft.com/.default"]

    def __init__(self, config: Config, *, app=None, session=None, sleep=None, max_attempts=4):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        self.config = config
        self._sleep = time.sleep if sleep is None else sleep
        self.max_attempts = max_attempts
        self._owns_session = session is None
        self.session = requests.Session() if session is None else session
        try:
            self.app = app if app is not None else msal.ConfidentialClientApplication(
                client_id=config.client_id,
                client_credential=config.client_secret,
                authority=f"https://login.microsoftonline.com/{config.tenant_id}",
                timeout=(10, 30),
            )
        except (requests.RequestException, ValueError):
            self.close()
            raise GraphError("Could not initialize authentication. Check the tenant ID and network connection.") from None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._owns_session:
            self.session.close()

    def _access_token(self):
        try:
            result = self.app.acquire_token_for_client(scopes=self.SCOPES)
        except (requests.RequestException, ValueError):
            raise GraphError("Application authentication could not complete. Check the network and app settings.") from None
        token = result.get("access_token")
        if not token:
            # Deliberately omit error_description and raw authentication results.
            error = result.get("error", "unknown_error")
            raise GraphError(f"Application authentication failed ({error}). Check the client secret Value and app configuration.")
        return token

    def _url(self, path):
        if path.startswith("/"):
            url = self.BASE_URL + path
        else:
            url = path
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "graph.microsoft.com"
            or not parsed.path.startswith("/v1.0/")
            or parsed.fragment
        ):
            raise GraphError("Refusing a URL outside Microsoft Graph v1.0.")
        return url

    @staticmethod
    def _retry_delay(header, attempt):
        if header is not None:
            try:
                seconds = float(header)
                if math.isfinite(seconds) and seconds >= 0:
                    return seconds
            except (ValueError, TypeError):
                try:
                    retry_at = parsedate_to_datetime(header)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        return min(2 ** attempt, 60)

    def get(self, path, *, params=None):
        url = self._url(path)
        for attempt in range(self.max_attempts):
            token = self._access_token()
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    timeout=(10, 30),
                    allow_redirects=False,
                )
            except requests.RequestException:
                raise GraphError("Graph could not be reached. Check the network, proxy, and connection timeout.") from None

            if response.status_code == 429 and attempt + 1 < self.max_attempts:
                delay = self._retry_delay(response.headers.get("Retry-After"), attempt)
                response.close()
                self._sleep(delay)
                continue

            if not 200 <= response.status_code < 300:
                status = response.status_code
                response.close()
                hints = {
                    401: "Authentication was rejected. Rerun the connection check to obtain a new token.",
                    403: "Check application permissions and administrator consent for this operation.",
                    404: "Check the resource ID and tenant.",
                    429: "The bounded retry limit was reached. Retry later.",
                }
                raise GraphError(f"Graph GET failed: HTTP {status}. " + hints.get(status, "Inspect the request and service availability."))

            try:
                body = response.json()
            except ValueError:
                raise GraphError("Graph returned an unexpected non-JSON response.") from None
            finally:
                response.close()
            if not isinstance(body, dict):
                raise GraphError("Graph returned an unexpected response format.")
            return body
        raise GraphError("Graph request did not complete.")

    def get_all(self, path, *, params=None):
        """Follow Graph pagination, checking the destination before every request."""
        items = []
        seen = set()
        while path:
            if path in seen:
                raise GraphError("Graph returned a repeated pagination link.")
            seen.add(path)
            page = self.get(path, params=params)
            values = page.get("value")
            if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
                raise GraphError("Graph returned an unexpected collection format.")
            items.extend(values)
            path = page.get("@odata.nextLink")
            if path is not None and not isinstance(path, str):
                raise GraphError("Graph returned an invalid pagination link.")
            params = None  # nextLink already includes its query string.
        return items
