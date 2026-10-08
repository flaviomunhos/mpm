"""Lado TX (máquina antiga): conecta ao RX e responde, somente leitura.

O TX nunca grava nada nas pastas do usuário. Só serve arquivos que estão
dentro das pastas do perfil escolhido pelo RX (Desktop, Documents, Pictures).

Conexões de trabalho (cópia em paralelo): a pedido do RX, o TX abre N conexões
extras de volta ao MESMO endereço que ele já usou (nunca outro destino). Elas
se autenticam com um segredo de sessão que o RX enviou pelo canal já
autenticado e só aceitam operações de leitura (fetch, ping, bench, bye).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import tempfile
import threading
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from mpm.core.manifest import build_manifest
from mpm.core.models import KNOWN_FOLDER_KEYS, ProfileInfo
from mpm.core.profiles import list_profiles, select_profile
from mpm.core.system import discover_system
from mpm import platforms
from mpm.core.user import discover_user
from mpm.net.handshake import authenticate_as_tx
from mpm.net.protocol import CHUNK_SIZE, recv_msg, send_msg
from mpm.core.longpath import long_path
from mpm.net.safepath import resolve_in_root
from mpm.net.security import make_client_context

MAX_WORKERS = 32
MAX_BENCH_BYTES = 512 * 1024 * 1024
WORKER_OPS = frozenset({"fetch", "ping", "bench", "bye"})


# Atributos do Windows preservados na migração: somente leitura, oculto, sistema.
# (desktop.ini sem Oculto+Sistema aparece visível no Desktop e nas pastas.)
PRESERVED_ATTRS = 0x1 | 0x2 | 0x4


def _file_attrs(st: os.stat_result) -> int:
    return int(getattr(st, "st_file_attributes", 0)) & PRESERVED_ATTRS     # só existe no Windows


def _tune(raw: socket.socket) -> None:
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # sem isso: ~40 ms por arquivo pequeno


class TxShared:
    """Estado comum à conexão principal e às conexões de trabalho."""

    def __init__(self, profile: ProfileInfo | None):
        self.fixed_profile = profile
        self.roots: dict[str, Path] = {}
        self.inventory_path: Path | None = None
        self.workdir = tempfile.TemporaryDirectory(prefix="mpm-tx-")


class TxSession:
    def __init__(
        self,
        conn: socket.socket,
        *,
        profile: ProfileInfo | None = None,
        log: Callable[[str], None] = print,
        shared: TxShared | None = None,
        allowed_ops: frozenset[str] | None = None,
        endpoint: tuple[str, int] | None = None,
    ):
        self.conn = conn
        self.log = log
        self.shared = shared or TxShared(profile)
        self._owner = shared is None            # só quem criou o estado o apaga
        self.allowed_ops = allowed_ops
        self.endpoint = endpoint

    # ------------------------------------------------------------------ loop
    def serve(self) -> None:
        try:
            while True:
                try:
                    header, _ = recv_msg(self.conn)
                except ConnectionError:
                    return
                op = header.get("op")
                if op == "bye":
                    send_msg(self.conn, {"ok": True})
                    return
                if not isinstance(op, str) or not op.isidentifier():
                    send_msg(self.conn, {"ok": False, "error": f"operação desconhecida: {op!r}"})
                    continue
                if self.allowed_ops is not None and op not in self.allowed_ops:
                    send_msg(self.conn, {"ok": False, "error": f"operação não permitida aqui: {op!r}"})
                    continue
                handler = getattr(self, f"op_{op}", None)
                if handler is None:
                    send_msg(self.conn, {"ok": False, "error": f"operação desconhecida: {op!r}"})
                    continue
                try:
                    handler(header)
                except (OSError, ValueError, KeyError) as exc:
                    try:
                        send_msg(self.conn, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                    except OSError:
                        return
        finally:
            if self._owner:
                self.shared.workdir.cleanup()

    # ------------------------------------------------------------ operations
    def _profiles(self) -> list[ProfileInfo]:
        fixed = self.shared.fixed_profile
        return [fixed] if fixed else list_profiles()

    def op_info(self, header: dict[str, Any]) -> None:
        send_msg(self.conn, {
            "ok": True,
            "system": discover_system().to_dict(),
            "user": discover_user().to_dict(),
            "profiles": [p.to_dict() for p in self._profiles()],
        })

    def op_scan(self, header: dict[str, Any]) -> None:
        profile = self.shared.fixed_profile or select_profile(header.get("user"))
        self.log(f"Varrendo perfil {profile.username} ...")
        inventory = Path(self.shared.workdir.name) / "inventory.jsonl"
        manifest = build_manifest(
            profile=profile, include_appdata=False, inventory_path=inventory,
        )
        self.shared.roots = {
            key: Path(info["path"])
            for key, info in manifest.folders.items()
            if info["exists"] and key in KNOWN_FOLDER_KEYS
        }
        self.shared.inventory_path = inventory
        send_msg(self.conn, {"ok": True, "manifest": manifest.to_dict()})

    def op_get_inventory(self, header: dict[str, Any]) -> None:
        path = self.shared.inventory_path
        if not path or not path.is_file():
            raise ValueError("faça 'scan' antes de pedir o inventário")
        with path.open("rb") as fh:
            self._stream(fh)

    def op_fetch(self, header: dict[str, Any]) -> None:
        folder, rel = header.get("folder"), header.get("path")
        root = self.shared.roots.get(folder) if isinstance(folder, str) else None
        if root is None:
            raise ValueError("pasta desconhecida (faça 'scan' antes)")
        offset = header.get("offset", 0)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("offset inválido")
        target = long_path(resolve_in_root(root, rel))
        with target.open("rb") as fh:
            before = os.fstat(fh.fileno())
            if offset > before.st_size:
                raise ValueError("offset maior que o arquivo")
            self._stream(fh, before=before, offset=offset)

    def op_ping(self, header: dict[str, Any]) -> None:
        send_msg(self.conn, {"ok": True, "t": header.get("t")})

    def op_bench(self, header: dict[str, Any]) -> None:
        """Envia `size` bytes sem tocar no disco: mede só a rede (e o TLS)."""
        size = header.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_BENCH_BYTES:
            raise ValueError("tamanho de teste inválido")
        block = os.urandom(CHUNK_SIZE)
        sent = 0
        while sent < size:
            piece = block[: min(CHUNK_SIZE, size - sent)]
            send_msg(self.conn, {"ok": True, "chunk": True}, piece)
            sent += len(piece)
        send_msg(self.conn, {"ok": True, "end": True, "size": sent})

    def op_list_apps(self, header: dict[str, Any]) -> None:
        """Inventário de aplicativos (somente leitura): registro + `winget export`."""
        backend = platforms.apps()
        reply: dict[str, Any] = {"ok": True, "apps": [], "winget": None, "errors": []}
        try:
            reply["apps"] = backend.list_installed_apps()
        except Exception as exc:       # noqa: BLE001 - erro vira aviso no relatório do RX
            reply["errors"].append(f"registro: {type(exc).__name__}: {exc}")
        if header.get("winget", True):
            try:
                reply["winget"] = backend.winget_export()
            except Exception as exc:   # noqa: BLE001
                reply["errors"].append(f"winget: {type(exc).__name__}: {exc}")
        send_msg(self.conn, reply)

    def _settings_profile(self, header: dict[str, Any]) -> ProfileInfo | None:
        """Perfil pedido para ler as configurações; None = o usuário que executa o TX."""
        name = header.get("user")
        if not name:
            return None
        if not isinstance(name, str):
            raise ValueError("perfil inválido")
        profile = self.shared.fixed_profile or select_profile(name)
        return None if profile.is_current else profile

    def op_scan_appdata(self, header: dict[str, Any]) -> None:
        """Mede os dados/configurações dos programas de um perfil (somente leitura)."""
        from mpm.core.appdata import PLUGINS, scan
        backend = platforms.appdata()
        profile = self._settings_profile(header)
        reply: dict[str, Any] = {"ok": True, "results": [], "profile": "", "errors": []}
        try:
            if profile is None:
                roots = backend.roots()
                reply["profile"] = str(roots.get("profile", ""))
                reply["results"] = scan(PLUGINS, roots, backend.registry_key_exists)
            else:
                roots = backend.roots_for(Path(profile.path))
                reply["profile"] = str(roots["profile"])
                try:
                    with platforms.regtools().user_hive(profile.sid, Path(profile.path) / "NTUSER.DAT") as hive:
                        reply["results"] = scan(PLUGINS, roots, lambda key: backend.registry_key_exists(key, hive))
                except RuntimeError as exc:           # sem o registro do usuário: só as pastas
                    reply["errors"].append(f"{exc}; as chaves de registro (PuTTY, WinSCP...) ficam de fora")
                    reply["results"] = scan(PLUGINS, roots, lambda key: False)
        except Exception as exc:       # noqa: BLE001 - erro vira aviso no relatório do RX
            reply["errors"].append(f"{type(exc).__name__}: {exc}")
        send_msg(self.conn, reply)

    def op_scan_settings(self, header: dict[str, Any]) -> None:
        """Inventário arquivo a arquivo das pastas dos programas escolhidos (somente leitura)."""
        from mpm.core import appdata as ad
        ids = header.get("plugins")
        if not isinstance(ids, list) or not 0 < len(ids) <= 128 or not all(isinstance(i, str) for i in ids):
            raise ValueError("lista de programas inválida")
        profile = self._settings_profile(header)
        backend = platforms.appdata()
        roots = backend.roots() if profile is None else backend.roots_for(Path(profile.path))
        entries, folder_roots, info = ad.inventory(ids, roots)       # ValueError se algum não for do catálogo
        path = Path(self.shared.workdir.name) / "settings-inventory.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=True) + "\n")
        self.shared.roots = folder_roots
        self.shared.inventory_path = path
        self.log(f"Inventário das configurações: {len(entries)} arquivos")
        send_msg(self.conn, {"ok": True, "manifest": {
            "migration_id": ad.new_migration_id(), "folders": info, "files": len(entries),
            "bytes": sum(e["size"] for e in entries)}})

    def op_export_registry(self, header: dict[str, Any]) -> None:
        """Exporta (.reg, em base64) só chaves HKCU do catálogo de programas."""
        from mpm.core import appdata as ad
        keys = header.get("keys")
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys) or len(keys) > 64:
            raise ValueError("lista de chaves inválida")
        allowed = ad.registry_allowlist()
        bad = [k for k in keys if k not in allowed]
        if bad:
            raise ValueError(f"chave fora do catálogo: {bad[0]!r}")
        backend = platforms.regtools()
        profile = self._settings_profile(header)
        reply: dict[str, Any] = {"ok": True, "exports": {}, "errors": []}
        total = 0
        try:
            cm = (nullcontext(None) if profile is None
                  else backend.user_hive(profile.sid, Path(profile.path) / "NTUSER.DAT"))
            with cm as hive:
                for key in keys:
                    try:
                        data = backend.export_key(key) if hive is None else backend.export_key(key, hive)
                    except Exception as exc:    # noqa: BLE001 - vira aviso no relatório do RX
                        reply["errors"].append(f"{key}: {type(exc).__name__}: {exc}")
                        continue
                    if data is None:
                        continue                # chave ausente neste usuário
                    total += len(data)
                    if total > 8 * 1024 * 1024:
                        reply["errors"].append(f"{key}: exportações somam mais de 8 MB; esta ficou de fora")
                        continue
                    reply["exports"][key] = base64.b64encode(data).decode("ascii")
        except RuntimeError as exc:             # não abriu o registro do usuário
            reply["errors"].append(str(exc))
        send_msg(self.conn, reply)

    def op_scan_devices(self, header: dict[str, Any]) -> None:
        """Perfis de Wi-Fi do TX (somente leitura; o resumo não leva senha)."""
        from mpm.core import devices as dv
        backend = platforms.devices()
        reply: dict[str, Any] = {"ok": True, "wifi": [], "elevated": None, "errors": []}
        try:
            reply["elevated"] = bool(backend.is_elevated())
        except Exception:                      # noqa: BLE001
            pass
        try:
            for xml in backend.export_wifi_xml():
                try:
                    reply["wifi"].append(dv.parse_wifi_xml(xml))
                except ValueError as exc:
                    reply["errors"].append(f"wifi: perfil ignorado ({exc})")
        except Exception as exc:               # noqa: BLE001
            reply["errors"].append(f"wifi: {type(exc).__name__}: {exc}")
        send_msg(self.conn, reply)

    def op_export_wifi(self, header: dict[str, Any]) -> None:
        """XML (com senha) só dos perfis pedidos pelo nome. Só trafega pelo canal TLS do pareamento."""
        from mpm.core import devices as dv
        names = header.get("names")
        if not isinstance(names, list) or not names or len(names) > 256 or not all(isinstance(n, str) for n in names):
            raise ValueError("lista de redes inválida")
        wanted = {n.casefold() for n in names}
        profiles, errors = [], []
        for xml in platforms.devices().export_wifi_xml():
            try:
                info = dv.parse_wifi_xml(xml)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if info["name"].casefold() in wanted:
                profiles.append({"name": info["name"], "xml": xml})
        send_msg(self.conn, {"ok": True, "profiles": profiles, "errors": errors})

    def op_open_workers(self, header: dict[str, Any]) -> None:
        count, secret = header.get("count"), header.get("secret")
        if (not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_WORKERS):
            raise ValueError(f"número de conexões inválido (1 a {MAX_WORKERS})")
        if not isinstance(secret, str) or not 16 <= len(secret) <= 128 or not secret.isalnum():
            raise ValueError("segredo de sessão inválido")
        if self.endpoint is None:
            raise ValueError("esta conexão não pode abrir conexões de trabalho")
        host, port = self.endpoint
        for _ in range(count):
            threading.Thread(
                target=self._worker_main, args=(host, port, secret),
                name="mpm-tx-worker", daemon=True,
            ).start()
        send_msg(self.conn, {"ok": True, "count": count})

    def _worker_main(self, host: str, port: int, secret: str) -> None:
        try:
            raw = socket.create_connection((host, port), timeout=15)
            _tune(raw)
            with make_client_context().wrap_socket(raw) as conn:
                authenticate_as_tx(conn, secret)
                conn.settimeout(None)
                TxSession(conn, log=lambda _m: None, shared=self.shared,
                          allowed_ops=WORKER_OPS).serve()
        except Exception as exc:     # noqa: BLE001 - a thread não pode propagar; o RX nota a falta
            self.log(f"Conexão de trabalho encerrada: {type(exc).__name__}: {exc}")

    # --------------------------------------------------------------- helpers
    def _stream(self, fh, *, before: os.stat_result | None = None, offset: int = 0) -> None:
        digest = hashlib.sha256()
        # o hash cobre o arquivo INTEIRO: o prefixo já recebido pelo RX é lido aqui,
        # mas não reenviado; o RX confere o arquivo final contra este hash.
        remaining = offset
        while remaining:
            block = fh.read(min(CHUNK_SIZE, remaining))
            if not block:
                raise ValueError("arquivo menor que o offset pedido")
            digest.update(block)
            remaining -= len(block)

        total = offset
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            send_msg(self.conn, {"ok": True, "chunk": True}, chunk)

        end: dict[str, Any] = {"ok": True, "end": True, "sha256": digest.hexdigest(),
                               "size": total, "offset": offset}
        if before is not None:
            after = os.fstat(fh.fileno())
            end["mtime_ns"] = before.st_mtime_ns
            attrs = _file_attrs(before)
            if attrs:
                end["attrs"] = attrs
            end["changed"] = (
                total != before.st_size
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
            )
        send_msg(self.conn, end)


def run_tx(
    host: str,
    port: int,
    code: str,
    *,
    profile: ProfileInfo | None = None,
    log: Callable[[str], None] = print,
    connect_timeout: float = 15.0,
) -> None:
    """Conecta ao RX, autentica e serve pedidos até o RX encerrar."""
    context = make_client_context()
    raw = socket.create_connection((host, port), timeout=connect_timeout)
    _tune(raw)
    with context.wrap_socket(raw) as conn:
        rx_version = authenticate_as_tx(conn, code)
        conn.settimeout(None)     # o RX pode ficar minutos entre passadas
        log(f"Pareado com o RX (MPM {rx_version}). Servindo em modo somente leitura.")
        TxSession(conn, profile=profile, log=log, endpoint=(host, port)).serve()
    log("Sessão encerrada.")
