"""Fuobe Connect's own small file: the studios this ComfyUI trusts, and its pairing (the studio and its token).

It lives in ComfyUI's user folder, beside (not inside) the folder ComfyUI serves as user data. Only your own user
account may read it: mode 0600 on Linux and macOS, and on Windows an access list that names only your account (no
inherited entries), set before the token is written.
"""

from __future__ import annotations

import json
import logging
import os
import threading

FILE_NAME = "fuobe-connect.json"

log = logging.getLogger("fuobe_connect")


def _current_user_sid() -> str | None:
    """This Windows account's SID, as text (S-1-5-21-...)."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    TOKEN_QUERY = 0x0008
    TOKEN_USER = 1
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        need = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(need))
        buf = ctypes.create_string_buffer(need.value)
        if not advapi32.GetTokenInformation(token, TOKEN_USER, buf, need, ctypes.byref(need)):
            return None
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


def owner_only(path: str) -> bool:
    """Windows: give the file one access entry, full control for this account, and no inherited ones. True when set."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    sid = _current_user_sid()
    if not sid:
        return False
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
    advapi32.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    sd = ctypes.c_void_p()
    # D:P = a protected access list (nothing inherited from the folder); one entry: full access (FA) for this account.
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(f"D:P(A;;FA;;;{sid})", 1, ctypes.byref(sd), None):
        return False
    try:
        DACL_SECURITY_INFORMATION = 0x00000004
        PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
        return bool(advapi32.SetFileSecurityW(path, DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION, sd))
    finally:
        kernel32.LocalFree(sd)


def access_list(path: str) -> str | None:
    """Windows: the file's access list as SDDL text (for the tests and a check)."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.ULONG)]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    DACL = 0x00000004
    need = wintypes.DWORD(0)
    advapi32.GetFileSecurityW(path, DACL, None, 0, ctypes.byref(need))
    buf = ctypes.create_string_buffer(need.value)
    if not advapi32.GetFileSecurityW(path, DACL, buf, need, ctypes.byref(need)):
        return None
    text = wintypes.LPWSTR()
    if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(buf, 1, DACL, ctypes.byref(text), None):
        return None
    try:
        return text.value
    finally:
        kernel32.LocalFree(text)


class Store:
    def __init__(self, folder: str):
        self.path = os.path.join(folder, FILE_NAME)
        self._lock = threading.Lock()
        self._warned = False

    def load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def update(self, **changes) -> dict:
        """Change some keys (None removes one) and save at once."""
        with self._lock:
            data = self.load()
            for k, v in changes.items():
                if v is None:
                    data.pop(k, None)
                else:
                    data[k] = v
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            try:
                os.remove(tmp)
            except OSError:
                pass
            # A new file of our own (never one someone placed there), private before anything is written to it.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            try:
                if os.name == "nt" and not owner_only(tmp) and not self._warned:
                    self._warned = True
                    log.warning("[Fuobe Connect] Couldn't make %s private to your Windows account.", self.path)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    fd = None
                    json.dump(data, f, indent=2)
            finally:
                if fd is not None:
                    os.close(fd)
            os.replace(tmp, self.path)
            if os.name != "nt":
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            return data
