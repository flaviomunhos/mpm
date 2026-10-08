"""Backend Linux (ambiente de desenvolvimento e testes)."""

from mpm.platforms.linux.profiles import appdata_roots, known_folders, list_profiles
from mpm.platforms.linux.system import discover_os
from mpm.platforms.linux.users import discover_user

__all__ = ["appdata_roots", "discover_os", "discover_user", "known_folders", "list_profiles"]
