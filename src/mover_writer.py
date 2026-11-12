"""Scoped mover writes. Account deletion, creation and password changes are absent."""

import re

import requests

from .graph_client import GraphClient
from .graph_writer import WriteError


GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


class MoverWriter(GraphClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.write_attempts = 0
        self.writes_succeeded = 0
        self.users, self.groups, self.managers = set(), set(), set()

    def set_scope(self, items):
        self.users = {item.user_id for item in items}
        self.groups = {value["id"] for item in items for value in item.groups.values()}
        self.managers = {item.manager["id"] for item in items}

    def write(self, method, path, body=None):
        remove = re.fullmatch(rf"/groups/({GUID})/members/({GUID})/\$ref", path)
        add = re.fullmatch(rf"/groups/({GUID})/members/\$ref", path)
        manager = re.fullmatch(rf"/users/({GUID})/manager/\$ref", path)
        patch = re.fullmatch(rf"/users/({GUID})", path)
        allowed = False
        if method == "DELETE" and remove:
            allowed = remove[1] in self.groups and remove[2] in self.users and body is None
        elif method == "POST" and add and isinstance(body, dict):
            allowed = add[1] in self.groups and body in [
                {"@odata.id": self.BASE_URL + "/directoryObjects/" + identifier} for identifier in self.users]
        elif method == "PUT" and manager and isinstance(body, dict):
            allowed = manager[1] in self.users and body in [
                {"@odata.id": self.BASE_URL + "/users/" + identifier} for identifier in self.managers]
        elif method == "PATCH" and patch and isinstance(body, dict):
            allowed = (patch[1] in self.users and bool(body)
                       and set(body) <= {"department", "jobTitle"}
                       and all(isinstance(value, str) and value for value in body.values()))
        if not allowed:
            raise WriteError("This write is outside the validated mover scope. Group DELETE requires /$ref.")
        for attempt in range(self.max_attempts):
            token = self._access_token()
            kwargs = dict(headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                          timeout=(10, 30), allow_redirects=False)
            if body is not None:
                kwargs["json"] = body
            self.write_attempts += 1
            try:
                response = self.session.request(method, self._url(path), **kwargs)
            except requests.RequestException:
                raise WriteError("Mover write connection failed; its outcome is unknown. Preserve the journal and rerun a dry run before recovery.", uncertain=True) from None
            status = response.status_code
            if status == 429 and attempt + 1 < self.max_attempts:
                delay = self._retry_delay(response.headers.get("Retry-After"), attempt)
                response.close()
                self._sleep(delay)
                continue
            try:
                if status != 204:
                    raise WriteError(f"Graph {method} failed: HTTP {status}. The mover stopped; inspect state before resuming.",
                                     status=status, uncertain=status >= 500 or status == 408 or 200 <= status < 300)
                self.writes_succeeded += 1
                return None, status
            finally:
                response.close()
        raise WriteError("Mover write did not complete.")
