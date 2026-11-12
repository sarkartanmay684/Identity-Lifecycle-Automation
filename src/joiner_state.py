"""Windows-protected local credentials and an atomic provisioning journal."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import secrets
import tempfile


class StateError(RuntimeError):
    pass


class WindowsProtector:
    """DPAPI current-user protection; no machine-wide key or plaintext fallback."""

    def _transform(self, data: bytes, decrypt: bool) -> bytes:
        if os.name != "nt":
            raise StateError("Joiner apply/password storage requires Windows in this checkpoint.")
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        buffer = ctypes.create_string_buffer(data)
        incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        outgoing = Blob()
        try:
            if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
                raise StateError("Windows credential protection failed. Use the same Windows account and computer.")
            return ctypes.string_at(outgoing.pbData, outgoing.cbData)
        finally:
            ctypes.memset(buffer, 0, len(buffer))
            if outgoing.pbData:
                ctypes.memset(outgoing.pbData, 0, outgoing.cbData)
                kernel32.LocalFree(ctypes.cast(outgoing.pbData, ctypes.c_void_p))

    def protect(self, data):
        return self._transform(data, False)

    def unprotect(self, data):
        return self._transform(data, True)

    def self_check(self):
        probe = secrets.token_bytes(32)
        if self.unprotect(self.protect(probe)) != probe:
            raise StateError("Windows credential protection self-check failed.")


def state_folder(config):
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "IdentityLifecycleAutomation" / config.tenant_id
    if os.name == "nt":
        raise StateError("LOCALAPPDATA is unavailable; cannot locate the private state directory.")
    # Read-only commands can run elsewhere; apply still requires Windows DPAPI.
    return Path.home() / ".local" / "share" / "IdentityLifecycleAutomation" / config.tenant_id


class Journal:
    def __init__(self, config, *, folder=None, protector=None):
        self.folder = Path(folder) if folder is not None else state_folder(config)
        self.path = self.folder / "joiner-state.dpapi"
        self.lock_path = self.folder / "joiner-apply.lock"
        self.protector = protector or WindowsProtector()
        self.identity = {"version": 1, "tenant_id": config.tenant_id,
                         "client_id": config.client_id, "upn_domain": config.upn_domain}
        self.data = {**self.identity, "users": {}}
        self.locked = False

    def load(self):
        if not self.path.exists():
            return self
        try:
            value = json.loads(self.protector.unprotect(self.path.read_bytes()))
        except (ValueError, UnicodeError):
            raise StateError("The joiner journal cannot be read. Preserve it for recovery; do not delete it.") from None
        if not isinstance(value, dict) or any(value.get(key) != val for key, val in self.identity.items()) or not isinstance(value.get("users"), dict):
            raise StateError("The joiner journal does not match this tenant, application, or domain.")
        self.data = value
        return self

    def __enter__(self):
        self.protector.self_check()  # Fail before Graph writes if encryption is unavailable.
        self.folder.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise StateError("An apply lock exists. Check for a running process; follow the recovery guide if it is stale.") from None
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(str(os.getpid()))
            self.locked = True
            return self.load()
        except BaseException:
            self.lock_path.unlink(missing_ok=True)
            self.locked = False
            raise

    def __exit__(self, *args):
        if self.locked:
            self.lock_path.unlink(missing_ok=True)
            self.locked = False

    def save(self):
        if not self.locked:
            raise StateError("Journal writes require the apply lock.")
        protected = self.protector.protect(json.dumps(self.data).encode("utf-8"))
        descriptor, temporary = tempfile.mkstemp(prefix="joiner-state-", suffix=".tmp", dir=self.folder)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(protected)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def get(self, employee_id):
        value = self.data["users"].get(employee_id.casefold())
        if value is not None and not isinstance(value, dict):
            raise StateError("Invalid employee record in the protected journal.")
        return value

    def put(self, employee_id, value):
        self.data["users"][employee_id.casefold()] = value
        self.save()

    def forget_rejected_creation(self, employee_id):
        self.data["users"].pop(employee_id.casefold(), None)
        self.save()
