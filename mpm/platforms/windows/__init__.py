"""Backend Windows (alvo do produto final).

Usa apenas a biblioteca padrão (ctypes, subprocess, winreg) para
manter o MPM portátil. Importar este pacote em Linux é seguro:
nada específico do Windows é importado no nível do módulo.
"""

from mpm.platforms.windows.profiles import appdata_roots, known_folders, list_profiles
from mpm.platforms.windows.system import discover_os
from mpm.platforms.windows.users import discover_user

__all__ = ["appdata_roots", "discover_os", "discover_user", "known_folders", "list_profiles"]
