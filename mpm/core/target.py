"""Destino = perfil real de um usuário local da máquina nova (0.3).

Três fases, para que nada seja criado antes da confirmação:
  plan()     só descreve e valida; devolve os caminhos PREVISTOS (usado no dry-run)
  commit()   cria o usuário (se preciso) e o perfil; devolve os caminhos REAIS
  finalize() ao final, passa a propriedade dos arquivos copiados para o usuário

Retomada: o SID criado fica em <meta>/target.json. Se a cópia for interrompida e rodada
de novo, o mesmo usuário é reaproveitado. Um usuário que já existia antes do MPM só é
usado com merge=True (--merge), porque a cópia pode sobrescrever arquivos dele.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from mpm.core.accounts import AccountError, validate_username
from mpm.core.models import DEFAULT_FOLDER_NAMES, KNOWN_FOLDER_KEYS


class AccountTarget:
    def __init__(
        self,
        backend: ModuleType,
        *,
        name: str | None,
        admin: bool,
        merge: bool,
        meta_dir: Path,
        get_password: Callable[[str], str],
        log: Callable[[str], None] = print,
    ):
        self.backend = backend
        self.requested = name
        self.admin = admin
        self.merge = merge
        self.state_file = meta_dir / "target.json"
        self.get_password = get_password
        self.log = log
        self.name = ""
        self.sid: str | None = None
        self.roots: dict[str, Path] = {}

    @staticmethod
    def _roots(profile: Path) -> dict[str, Path]:
        return {key: profile / DEFAULT_FOLDER_NAMES[key] for key in KNOWN_FOLDER_KEYS}

    def _recorded_sid(self) -> str | None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return str(data["sid"]) if data.get("name", "").lower() == self.name.lower() else None
        except (OSError, ValueError, KeyError, AttributeError):
            return None

    # -------------------------------------------------------------- phase 1
    def plan(self, source_profile: str, *, dry_run: bool = False) -> dict[str, Path]:
        self.name = validate_username(self.requested or source_profile)
        elevated = self.backend.is_elevated()
        if elevated is False and not dry_run:
            raise AccountError("criar usuário exige Windows em modo Administrador "
                               "(abra o terminal com 'Executar como administrador')")
        self.sid = self.backend.lookup_user(self.name)
        self.log(f"\n[Destino] Usuário na máquina nova: {self.name}"
                 f"{' (administrador)' if self.admin else ' (usuário padrão)'}")
        if self.sid:
            if self._recorded_sid() == self.sid:
                self.log("  O usuário já foi criado por este MPM: retomando a migração.")
            elif self.merge:
                self.log("  AVISO: o usuário JÁ EXISTE; os arquivos serão copiados para o perfil dele "
                         "(arquivos de mesmo nome e tamanho/data diferentes serão substituídos).")
            else:
                raise AccountError(
                    f"o usuário {self.name!r} já existe nesta máquina. Escolha outro nome com "
                    "--as-user NOME ou, se quiser copiar para o perfil existente, use --merge")
            profile = self.backend.predict_profile_path(self.name)
        else:
            self.log("  O usuário NÃO existe: será criado agora, depois da sua confirmação.")
            self.log("  A senha não é migrada: você vai definir uma nova.")
            profile = self.backend.predict_profile_path(self.name)
        self.roots = self._roots(profile)
        self.log(f"  Perfil previsto: {profile}")
        for key in KNOWN_FOLDER_KEYS:
            self.log(f"      {key:<10} -> {self.roots[key]}")
        return dict(self.roots)

    # -------------------------------------------------------------- phase 2
    def commit(self) -> dict[str, Path]:
        if not self.name:
            raise AccountError("commit() antes de plan()")
        if not self.sid:
            password = self.get_password(self.name)
            self.log(f"Criando o usuário {self.name} ...")
            self.sid = self.backend.create_user(self.name, password, self.admin)
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps({"name": self.name, "sid": self.sid, "admin": self.admin}) + "\n",
                encoding="utf-8")
        profile = self.backend.ensure_profile(self.sid, self.name)
        self.roots = self._roots(Path(profile))
        for path in self.roots.values():
            path.mkdir(parents=True, exist_ok=True)
        self.log(f"Perfil pronto: {profile}")
        return dict(self.roots)

    # -------------------------------------------------------------- phase 3
    def finalize(self) -> None:
        if not self.roots:
            return
        self.log("Ajustando a propriedade dos arquivos para o usuário ...")
        problems = [msg for path in self.roots.values()
                    if (msg := self.backend.set_owner(path, self.name))]
        if problems:
            self.log(f"  Aviso: não consegui ajustar tudo ({problems[0]}). Os arquivos estão no "
                     "perfil e herdam as permissões da pasta; na dúvida, ajuste em "
                     "Propriedades > Segurança > Avançado > Proprietário.")
