"""Manifesto — o contrato entre SOURCE e AGENT (schema v1, rascunho).

Estrutura:
  source.system   máquina de origem
  source.user     quem executou o MPM
  source.profile  perfil selecionado para migração
  folders         resumo das pastas copiadas (Desktop/Documents/Pictures)
  appdata         resumo por aplicativo (inventário apenas; migração em outro módulo)
  inventory_file  JSONL com um registro por arquivo, ao lado do manifesto
  applications / settings   reservados (módulos futuros)
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mpm import MANIFEST_SCHEMA_VERSION, __version__
from mpm.core.filesystem import InventoryWriter, discover_appdata, discover_folders
from mpm.core.models import ProfileInfo, SystemInfo, UserInfo
from mpm.core.profiles import select_profile


def new_migration_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"MPM-{stamp}-{secrets.token_hex(3).upper()}"


@dataclass
class Manifest:
    source_system: SystemInfo
    source_user: UserInfo
    source_profile: ProfileInfo
    migration_id: str = field(default_factory=new_migration_id)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    mpm_version: str = __version__
    schema_version: int = MANIFEST_SCHEMA_VERSION
    folders: dict[str, Any] = field(default_factory=dict)
    appdata: dict[str, Any] = field(default_factory=dict)
    inventory_file: str | None = None
    applications: list[dict[str, Any]] = field(default_factory=list)
    settings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mpm_version": self.mpm_version,
            "migration_id": self.migration_id,
            "created_at": self.created_at,
            "source": {
                "system": self.source_system.to_dict(),
                "user": self.source_user.to_dict(),
                "profile": self.source_profile.to_dict(),
            },
            "folders": self.folders,
            "appdata": self.appdata,
            "inventory_file": self.inventory_file,
            "applications": self.applications,
            "settings": self.settings,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json() + "\n", encoding="utf-8")
        return target

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        if data.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version {data.get('schema_version')!r} não suportado "
                f"(esperado {MANIFEST_SCHEMA_VERSION})"
            )
        source = data["source"]
        return cls(
            source_system=SystemInfo(**source["system"]),
            source_user=UserInfo(**source["user"]),
            source_profile=ProfileInfo(**source["profile"]),
            migration_id=data["migration_id"],
            created_at=data["created_at"],
            mpm_version=data["mpm_version"],
            schema_version=data["schema_version"],
            folders=data.get("folders", {}),
            appdata=data.get("appdata", {}),
            inventory_file=data.get("inventory_file"),
            applications=data.get("applications", []),
            settings=data.get("settings", []),
        )

    @classmethod
    def read(cls, path: str | Path) -> "Manifest":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def build_manifest(
    selector: str | None = None,
    *,
    profile: ProfileInfo | None = None,
    include_appdata: bool = True,
    inventory_path: str | Path | None = None,
) -> Manifest:
    """Executa o Discovery e monta o manifesto.

    `profile` permite injetar um perfil já resolvido (testes, disco offline);
    caso contrário o perfil é escolhido por `selector` (padrão: usuário atual).
    """
    from mpm.core.system import discover_system
    from mpm.core.user import discover_user

    chosen = profile or select_profile(selector)

    writer = InventoryWriter(inventory_path) if inventory_path else None
    try:
        folders = discover_folders(chosen, writer)
    finally:
        if writer:
            writer.close()

    return Manifest(
        source_system=discover_system(),
        source_user=discover_user(),
        source_profile=chosen,
        folders={key: scan.to_dict() for key, scan in folders.items()},
        appdata=discover_appdata(chosen) if include_appdata else {},
        inventory_file=Path(inventory_path).name if inventory_path else None,
    )
