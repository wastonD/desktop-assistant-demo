# -*- coding: utf-8 -*-
"""凭据存取：Windows 凭据管理器（控制面板→凭据管理器→Windows 凭据里可见/可删）。

授权码、OAuth refresh token 都只存在这里，绝不写进 config/ 或 git。
用 ctypes 直调 advapi32，不引入 keyring 依赖。非 Windows（CI 测试）时退化为
环境变量 ASSISTANT_SECRET_<KEY>（只读）+ 进程内字典。
"""
import ctypes
import os
import re
import sys

TARGET_PREFIX = "DesktopAssistant/"
_memory: dict[str, str] = {}
use_memory = sys.platform != "win32"  # 测试时置 True，避免写进真实凭据管理器

if sys.platform == "win32":
    from ctypes import wintypes

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    _advapi.CredWriteW.restype = wintypes.BOOL
    _advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    _advapi.CredReadW.restype = wintypes.BOOL
    _advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi.CredDeleteW.restype = wintypes.BOOL
    _advapi.CredFree.argtypes = [ctypes.c_void_p]


def _target(key: str) -> str:
    return TARGET_PREFIX + key


def set_secret(key: str, value: str, username: str = "") -> None:
    if use_memory:
        _memory[key] = value
        return
    blob = value.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIALW()
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = _target(key)
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = username or key
    if not _advapi.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(ctypes.get_last_error(), "写入凭据管理器失败")


def get_secret(key: str) -> str | None:
    if use_memory:
        if key in _memory:
            return _memory[key]
        env = "ASSISTANT_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", key).upper()
        return os.environ.get(env)
    ptr = ctypes.POINTER(CREDENTIALW)()
    if not _advapi.CredReadW(_target(key), CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        if ctypes.get_last_error() == ERROR_NOT_FOUND:
            return None
        raise OSError(ctypes.get_last_error(), "读取凭据管理器失败")
    try:
        c = ptr.contents
        raw = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
        return raw.decode("utf-16-le")
    finally:
        _advapi.CredFree(ptr)


def delete_secret(key: str) -> None:
    if use_memory:
        _memory.pop(key, None)
        return
    if not _advapi.CredDeleteW(_target(key), CRED_TYPE_GENERIC, 0):
        if ctypes.get_last_error() != ERROR_NOT_FOUND:
            raise OSError(ctypes.get_last_error(), "删除凭据失败")


def password_key(account_id: str) -> str:
    return f"mail/{account_id}/password"


def token_key(account_id: str) -> str:
    return f"mail/{account_id}/oauth"
