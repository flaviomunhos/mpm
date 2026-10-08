"""Modelos de dados neutros de plataforma.

Tudo que o Discovery coleta é representado aqui, para que o core
não dependa de nenhum recurso específico de Linux ou Windows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Pastas do perfil que o MPM copia (decisão do projeto, out/2026).
# Downloads foi retirada do escopo em 03/10/2026 (volume alto, conteúdo reobtível).
# Chaves estáveis: usadas no manifesto e no inventário.
KNOWN_FOLDER_KEYS = ("desktop", "documents", "pictures")

# Nome real em disco (em pt-BR "Documentos"/"Imagens" são só nomes de
# exibição; no disco as pastas se chamam Documents/Pictures).
DEFAULT_FOLDER_NAMES = {
    "desktop": "Desktop",
    "documents": "Documents",
    "pictures": "Pictures",
}


@dataclass
class SystemInfo:
    hostname: str
    os_family: str          # "Linux" | "Windows" | "Darwin"
    os_name: str            # nome amigável, ex.: "Windows 10 Pro", "Debian GNU/Linux 13 (trixie)"
    os_release: str         # release / versão do SO
    os_build: str | None    # build (Windows) ou kernel (Linux)
    architecture: str
    python_version: str
    platform_string: str
    collected_at: str       # ISO 8601 UTC

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UserInfo:
    """Quem está executando o MPM."""
    username: str
    user_id: str            # UID (POSIX) ou SID (Windows)
    group_id: str | None    # GID (POSIX); None no Windows
    profile_path: str       # home (POSIX) ou C:\Users\<nome> (Windows)
    domain: str | None      # domínio ou workgroup/hostname
    domain_joined: bool | None = None
    shell: str | None = None
    is_elevated: bool | None = None   # root (POSIX) ou sessão elevada (Windows)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProfileInfo:
    """Um perfil de usuário existente na máquina (candidato à migração)."""
    username: str
    sid: str                # SID (Windows) ou UID (POSIX)
    path: str
    is_current: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class KnownFolder:
    """Resultado da resolução de uma pasta do perfil pela plataforma."""
    key: str
    path: str
    resolved_by: str        # "known-folder-api" | "registry" | "xdg" | "default"


@dataclass
class FolderScan:
    """Resumo da varredura de uma pasta (sem a lista de arquivos)."""
    key: str
    path: str
    exists: bool
    resolved_by: str
    redirected: bool        # caminho diferente do padrão (ex.: OneDrive Known Folder Move)
    file_count: int = 0
    dir_count: int = 0
    total_bytes: int = 0
    cloud_placeholders: int = 0   # arquivos só-na-nuvem (OneDrive); ler = baixar
    skipped_count: int = 0        # links simbólicos / junctions (não seguidos)
    skipped: list[dict[str, str]] = field(default_factory=list)   # amostra
    error_count: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)    # amostra

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
