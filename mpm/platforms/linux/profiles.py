from __future__ import annotations

import os
import pwd
from pathlib import Path

from mpm.core.models import (
    DEFAULT_FOLDER_NAMES,
    KNOWN_FOLDER_KEYS,
    KnownFolder,
    ProfileInfo,
)

_NO_LOGIN_SHELLS = {"nologin", "false"}
_XDG_KEYS = {
    "desktop": "XDG_DESKTOP_DIR",
    "documents": "XDG_DOCUMENTS_DIR",
    "pictures": "XDG_PICTURES_DIR",
}


def list_profiles() -> list[ProfileInfo]:
    current = os.getuid()
    profiles: list[ProfileInfo] = []
    for entry in pwd.getpwall():
        if entry.pw_uid != 0 and entry.pw_uid < 1000:
            continue
        if entry.pw_uid == 65534:  # nobody
            continue
        if Path(entry.pw_shell).name in _NO_LOGIN_SHELLS:
            continue
        if not Path(entry.pw_dir).is_dir():
            continue
        profiles.append(ProfileInfo(
            username=entry.pw_name,
            sid=str(entry.pw_uid),
            path=entry.pw_dir,
            is_current=(entry.pw_uid == current),
        ))
    return profiles


def _read_xdg(home: Path) -> dict[str, str]:
    """Lê ~/.config/user-dirs.dirs (XDG_*_DIR="$HOME/Pasta")."""
    result: dict[str, str] = {}
    cfg = home / ".config" / "user-dirs.dirs"
    if not cfg.is_file():
        return result
    for line in cfg.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip().strip('"').replace("$HOME", str(home))
        if value.startswith(str(home)):
            result[name.strip()] = value
    return result


def known_folders(profile: ProfileInfo) -> dict[str, KnownFolder]:
    home = Path(profile.path)
    xdg = _read_xdg(home)
    out: dict[str, KnownFolder] = {}
    for key in KNOWN_FOLDER_KEYS:
        xdg_path = xdg.get(_XDG_KEYS[key])
        if xdg_path:
            out[key] = KnownFolder(key, xdg_path, "xdg")
        else:
            out[key] = KnownFolder(key, str(home / DEFAULT_FOLDER_NAMES[key]), "default")
    return out


def appdata_roots(profile: ProfileInfo) -> dict[str, Path]:
    """Linux não tem AppData; o conceito equivalente fica fora do escopo."""
    return {}
