"""Write transport used only by explicit joiner apply commands."""

import re

import requests

from .graph_client import GraphClient, GraphError


class WriteError(GraphError):
    def __init__(self, message, *, status=None, uncertain=False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain


class GraphWriter(GraphClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.write_attempts = 0
        self.writes_succeeded = 0

    def write(self, method, path, body):
        # This checkpoint intentionally has no PATCH/DELETE/general write method.
        guid = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        allowed = (
            method == "POST" and path == "/users"
            or method == "POST" and re.fullmatch(rf"/groups/{guid}/members/\$ref", path)
            or method == "PUT" and re.fullmatch(rf"/users/{guid}/manager/\$ref", path)
        )
        if not allowed:
            raise WriteError("This write operation is outside the joiner checkpoint.")
        expected = 201 if path == "/users" else 204
        url = self._url(path)
        for attempt in range(self.max_attempts):
            token = self._access_token()
            self.write_attempts += 1
            try:
                response = self.session.request(
                    method, url, json=body,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    timeout=(10, 30), allow_redirects=False,
                )
            except requests.RequestException:
                raise WriteError("Graph write connection failed. Its outcome is unknown; inspect state before retrying.", uncertain=True) from None
            status = response.status_code
            if status == 429 and attempt + 1 < self.max_attempts:
                delay = self._retry_delay(response.headers.get("Retry-After"), attempt)
                response.close()
                self._sleep(delay)
                continue
            try:
                if status != expected:
                    # Never retry a user POST after a timeout/5xx or surprising success.
                    uncertain = status >= 500 or 200 <= status < 300 or status == 408
                    raise WriteError(
                        f"Graph {method} failed: HTTP {status}. "
                        + ("Outcome unknown; inspect state before retrying." if uncertain else "Check the request, consent, or throttling status."),
                        status=status, uncertain=uncertain,
                    )
                if expected == 204:
                    result = None
                else:
                    try:
                        result = response.json()
                    except ValueError:
                        raise WriteError("Creation response could not be read; outcome unknown.", uncertain=True) from None
                    if not isinstance(result, dict):
                        raise WriteError("Creation response had an unexpected format; outcome unknown.", uncertain=True)
                self.writes_succeeded += 1
                return result, status
            finally:
                response.close()
        raise WriteError("Graph write did not complete.")
