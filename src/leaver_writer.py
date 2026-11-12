"""Allow only writes belonging to a validated lab termination plan."""

import re
import requests

from .graph_client import GraphClient
from .graph_writer import WriteError
from .mover_writer import GUID


class LeaverWriter(GraphClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.write_attempts = 0
        self.writes_succeeded = 0
        self.users, self.groups, self.skus, self.report_managers = set(), set(), {}, {}

    def set_scope(self, items):
        self.users = {item.user_id for item in items}
        self.groups = {g["id"] for item in items for g in item.groups.values()}
        self.skus = {item.user_id: set(item.before["licenses"]["direct"]) for item in items}
        self.report_managers = {
            report_id: item.replacement["id"]
            for item in items for report_id in item.reports
        }

    def write(self, method, path, body=None):
        account = re.fullmatch(rf"/users/({GUID})", path)
        revoke = re.fullmatch(rf"/users/({GUID})/revokeSignInSessions", path)
        license_path = re.fullmatch(rf"/users/({GUID})/assignLicense", path)
        remove = re.fullmatch(rf"/groups/({GUID})/members/({GUID})/\$ref", path)
        manager = re.fullmatch(rf"/users/({GUID})/manager/\$ref", path)
        expected, allowed = 204, False
        if method == "PATCH" and account:
            allowed = (account[1] in self.users and isinstance(body, dict)
                       and set(body) == {"accountEnabled"} and body["accountEnabled"] is False)
        elif method == "POST" and revoke:
            expected = 200
            allowed = revoke[1] in self.users and body is None
        elif method == "DELETE" and remove:
            allowed = remove[1] in self.groups and remove[2] in self.users and body is None
        elif method == "POST" and license_path and isinstance(body, dict):
            expected = 200
            removals = body.get("removeLicenses")
            allowed = (license_path[1] in self.users
                       and set(body) == {"addLicenses", "removeLicenses"}
                       and body["addLicenses"] == []
                       and isinstance(removals, list) and bool(removals)
                       and all(isinstance(value, str) for value in removals)
                       and len(set(removals)) == len(removals)
                       and set(removals) <= self.skus[license_path[1]])
        elif method == "PUT" and manager:
            replacement = self.report_managers.get(manager[1])
            allowed = replacement is not None and body == {
                "@odata.id": self.BASE_URL + "/users/" + replacement}
        if not allowed:
            raise WriteError("Write refused: outside the validated leaver scope. Membership DELETE requires /$ref.")
        for attempt in range(self.max_attempts):
            kwargs = dict(headers={"Authorization": "Bearer " + self._access_token(),
                                   "Accept": "application/json"},
                          timeout=(10, 30), allow_redirects=False)
            if body is not None:
                kwargs["json"] = body
            self.write_attempts += 1
            try:
                response = self.session.request(method, self._url(path), **kwargs)
            except requests.RequestException:
                raise WriteError("Leaver write connection failed; outcome unknown. Preserve the journal and inspect a fresh dry run before recovery.",
                                 uncertain=True) from None
            status = response.status_code
            if status == 429 and attempt + 1 < self.max_attempts:
                delay = self._retry_delay(response.headers.get("Retry-After"), attempt)
                response.close()
                self._sleep(delay)
                continue
            try:
                accepted = 200 <= status < 300 if revoke else status == expected
                if not accepted:
                    raise WriteError(f"Graph {method} failed: HTTP {status}. Leaver execution stopped.",
                                     status=status, uncertain=status >= 500 or status == 408 or 200 <= status < 300)
                value = None
                if expected == 200 and (not revoke or response.content):
                    try:
                        value = response.json()
                    except ValueError:
                        raise WriteError("Graph returned an unexpected write response. Inspect current state before resuming.",
                                         status=status, uncertain=True) from None
                    if not isinstance(value, dict) or (revoke and value.get("value") is not True):
                        raise WriteError("Graph did not confirm the requested operation.",
                                         status=status, uncertain=True)
                    if license_path and value.get("id", license_path[1]) != license_path[1]:
                        raise WriteError("Graph returned a different user for licence removal.",
                                         status=status, uncertain=True)
                self.writes_succeeded += 1
                return value, status
            finally:
                response.close()
        raise WriteError("The bounded write retry limit was reached.")
