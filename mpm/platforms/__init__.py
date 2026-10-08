"""Seleção da camada de plataforma.

O core nunca importa platforms.linux / platforms.windows diretamente;
ele pede ao `current()` o backend correto. Todo backend expõe:

    discover_os()                -> (nome, release, build)
    discover_user()              -> UserInfo
    list_profiles()              -> list[ProfileInfo]
    known_folders(profile)       -> dict[str, KnownFolder]
    appdata_roots(profile)       -> dict[str, Path]
"""

from __future__ import annotations

import sys
from types import ModuleType


def current_name() -> str:
    return "windows" if sys.platform == "win32" else "linux"


def current() -> ModuleType:
    """Retorna o módulo de plataforma ativo."""
    if sys.platform == "win32":
        from mpm.platforms import windows as backend
    else:
        from mpm.platforms import linux as backend
    return backend


def apps() -> ModuleType:
    """Módulo de inventário de aplicativos do SO ativo (ver mpm.core.apps)."""
    if sys.platform == "win32":
        from mpm.platforms.windows import apps as backend
    else:
        from mpm.platforms.linux import apps as backend
    return backend


def appdata() -> ModuleType:
    """Pastas/registro de dados de aplicativos do SO ativo (ver mpm.core.appdata)."""
    if sys.platform == "win32":
        from mpm.platforms.windows import appdata as backend
    else:
        from mpm.platforms.linux import appdata as backend
    return backend


def devices() -> ModuleType:
    """Impressoras e perfis de Wi-Fi do SO ativo (ver mpm.core.devices)."""
    if sys.platform == "win32":
        from mpm.platforms.windows import devices as backend
    else:
        from mpm.platforms.linux import devices as backend
    return backend


def regtools() -> ModuleType:
    """Exportar/importar registro e carregar hives do SO ativo."""
    if sys.platform == "win32":
        from mpm.platforms.windows import regtools as backend
    else:
        from mpm.platforms.linux import regtools as backend
    return backend


def accounts() -> ModuleType:
    """Módulo de contas do SO ativo (ver mpm.core.accounts para o contrato)."""
    if sys.platform == "win32":
        from mpm.platforms.windows import accounts as backend
    else:
        from mpm.platforms.linux import accounts as backend
    return backend
