from __future__ import annotations

import os
import re
from pathlib import Path

from mpm.core.models import (
    DEFAULT_FOLDER_NAMES,
    KNOWN_FOLDER_KEYS,
    KnownFolder,
    ProfileInfo,
)

_PROFILE_LIST = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
# S-1-5-21: contas locais / Microsoft / domínio.  S-1-12-1: Entra ID (Azure AD).
_USER_SID_PREFIXES = ("S-1-5-21-", "S-1-12-1-")

# KNOWNFOLDERID (para SHGetKnownFolderPath, usuário atual)
_KNOWN_FOLDER_IDS = {
    "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
}

# Nomes dos valores em HKU\<SID>\...\Explorer\User Shell Folders
_USER_SHELL_FOLDER_VALUES = {
    "desktop": ("Desktop",),
    "documents": ("Personal",),
    "pictures": ("My Pictures",),
}
_USER_SHELL_FOLDERS_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def list_profiles() -> list[ProfileInfo]:
    import winreg

    current = _norm(os.environ.get("USERPROFILE", ""))
    profiles: list[ProfileInfo] = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _PROFILE_LIST) as root:
        index = 0
        while True:
            try:
                sid = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            if not sid.startswith(_USER_SID_PREFIXES):
                continue
            try:
                with winreg.OpenKey(root, sid) as sub:
                    raw, _ = winreg.QueryValueEx(sub, "ProfileImagePath")
            except OSError:
                continue
            path = os.path.expandvars(raw)
            if not os.path.isdir(path):
                continue
            profiles.append(ProfileInfo(
                username=os.path.basename(path),
                sid=sid,
                path=path,
                is_current=(_norm(path) == current),
            ))
    return profiles


def _shell_known_folder(key: str) -> str | None:
    """SHGetKnownFolderPath para o usuário que está executando o MPM.

    Respeita redirecionamentos (ex.: OneDrive Known Folder Move).
    """
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    u = uuid.UUID(_KNOWN_FOLDER_IDS[key])
    guid = GUID(u.time_low, u.time_mid, u.time_hi_version,
                (ctypes.c_ubyte * 8)(*u.bytes[8:]))

    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    ole32 = ctypes.windll.ole32      # type: ignore[attr-defined]
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    ptr = ctypes.c_wchar_p()
    if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(ptr)) != 0:
        return None
    try:
        return ptr.value
    finally:
        ole32.CoTaskMemFree(ctypes.cast(ptr, ctypes.c_void_p))


def _registry_known_folder(profile: ProfileInfo, key: str) -> str | None:
    """Lê User Shell Folders de HKU\\<SID>; só existe se o hive estiver carregado."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_USERS, f"{profile.sid}\\{_USER_SHELL_FOLDERS_KEY}") as hive:
            for value_name in _USER_SHELL_FOLDER_VALUES[key]:
                try:
                    raw, _ = winreg.QueryValueEx(hive, value_name)
                except OSError:
                    continue
                expanded = re.sub(r"%USERPROFILE%", lambda _m: profile.path, str(raw), flags=re.I)
                expanded = os.path.expandvars(expanded)
                if "%" not in expanded:
                    return expanded
    except OSError:
        pass
    return None


def known_folders(profile: ProfileInfo) -> dict[str, KnownFolder]:
    out: dict[str, KnownFolder] = {}
    for key in KNOWN_FOLDER_KEYS:
        path = method = None
        if profile.is_current:
            path, method = _shell_known_folder(key), "known-folder-api"
        if not path:
            path, method = _registry_known_folder(profile, key), "registry"
        if not path:
            path = str(Path(profile.path) / DEFAULT_FOLDER_NAMES[key])
            method = "default"
        out[key] = KnownFolder(key, path, method or "default")
    return out


def appdata_roots(profile: ProfileInfo) -> dict[str, Path]:
    base = Path(profile.path) / "AppData"
    return {
        "roaming": base / "Roaming",
        "local": base / "Local",
        "locallow": base / "LocalLow",
    }
