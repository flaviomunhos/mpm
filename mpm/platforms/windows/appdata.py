"""Pastas e chaves de registro do usuário que executa o TX, ou de outro perfil (somente leitura)."""

from __future__ import annotations

import os
from pathlib import Path


def roots() -> dict[str, Path]:
    profile = Path(os.environ.get("USERPROFILE") or Path.home())
    appdata = Path(os.environ.get("APPDATA") or profile / "AppData" / "Roaming")
    local = Path(os.environ.get("LOCALAPPDATA") or profile / "AppData" / "Local")
    documents = profile / "Documents"
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            documents = Path(os.path.expandvars(str(winreg.QueryValueEx(key, "Personal")[0])))
    except OSError:
        pass                                  # sem redirecionamento: Documentos dentro do perfil
    return {"profile": profile, "appdata": appdata, "localappdata": local, "documents": documents}


def roots_for(profile: Path) -> dict[str, Path]:
    """Pastas de outro perfil (não é o usuário do TX): segue a estrutura padrão do Windows."""
    return {"profile": profile, "appdata": profile / "AppData" / "Roaming",
            "localappdata": profile / "AppData" / "Local", "documents": profile / "Documents"}


def registry_key_exists(key: str, hive: str | None = None) -> bool:
    """A chave existe? Com `hive`, procura em HKEY_USERS\\<hive> (perfil de outro usuário)."""
    import winreg
    try:
        if hive:
            with winreg.OpenKey(winreg.HKEY_USERS, f"{hive}\\{key}"):
                return True
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key):
            return True
    except OSError:
        return False
