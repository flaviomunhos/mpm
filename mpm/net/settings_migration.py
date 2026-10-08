"""RX: copia as pastas de configuração dos programas (0.5.1) com o mesmo motor da migração do perfil.

Reaproveita `Migration` (várias conexões, retomada, tentativas, SHA-256 por arquivo, atributos); muda só
de onde vêm os arquivos (inventário do catálogo de programas) e para onde vão (pastas do usuário de destino).
Os metadados ficam em <dest>/settings, separados dos da migração do perfil.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mpm.core.appdata import dest_base
from mpm.core.fmt import fmt_bytes
from mpm.net.migration import Migration, MigrationError, Options, PassResult, RemoteError, RxClient
from mpm.core.longpath import long_path
from mpm.net.safepath import resolve_in_root


class SettingsMigration(Migration):
    def __init__(self, client: RxClient, options: Options, *, plugin_ids: list[str],
                 dest_roots: dict[str, Path], source_user: str | None = None, **kwargs: Any):
        super().__init__(client, options, **kwargs)
        self.plugin_ids = list(plugin_ids)
        self.source_user = source_user
        self.dest_roots = dest_roots
        self.meta = options.dest / "settings"
        self._file_keys: set[str] = set()

    # ------------------------------------------------------------------ run
    def run(self) -> dict[str, Any]:
        self.meta.mkdir(parents=True, exist_ok=True)
        started = datetime.now(timezone.utc)
        results: list[PassResult] = []
        manifest: dict[str, Any] = {}
        dry: dict[str, Any] | None = None
        aborted: str | None = None
        summary: dict[str, Any] = {}
        self.log(f"Conexões de cópia: {len(self.workers) or 1}")
        try:
            for number in range(1, self.options.passes + 1):
                label = "Passada 1 (massa)" if number == 1 else (
                    "Passada final (diferenças)" if number == self.options.passes
                    else f"Passada {number} (diferenças)")
                manifest, entries = self._scan("")
                todo, result = self._plan(entries, label)
                self._print_plan(label, entries, todo, result)
                if number == 1:
                    self._check_space(todo)
                    if self.options.dry_run:
                        dry = {"ok": True, "dry_run": True, "to_copy": result.to_copy,
                               "bytes_to_copy": sum(e["size"] for e in todo)}
                        break
                    if not self.options.assume_yes and self.ask \
                            and not self.ask("Copiar as configurações para o usuário de destino?"):
                        raise MigrationError("cancelado pelo usuário")
                self._copy(todo, result)
                results.append(result)
                self._print_result(result)
        except (MigrationError, ConnectionError, RemoteError, OSError) as exc:
            aborted = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if dry is None:
                summary = self._write_report(manifest, "configurações", started, results, aborted)
        return dry if dry is not None else summary

    # ---------------------------------------------------------------- scan
    def _scan(self, profile: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self.log("\nPedindo ao TX o inventário das configurações ...")
        request: dict[str, Any] = {"op": "scan_settings", "plugins": self.plugin_ids}
        if self.source_user:
            request["user"] = self.source_user
        manifest = self.client.request(request)["manifest"]
        self._file_keys = {key for key, meta in manifest.get("folders", {}).items() if meta.get("file")}
        inventory = self.meta / "inventory.jsonl"
        digest = hashlib.sha256()
        with inventory.open("wb") as fh:
            def sink(block: bytes) -> None:
                fh.write(block)
                digest.update(block)
            end = self.client.stream({"op": "get_inventory"}, sink)
        if end["sha256"] != digest.hexdigest():
            raise MigrationError("inventário corrompido na transferência")
        entries = [json.loads(line) for line in inventory.read_text(encoding="utf-8").splitlines() if line.strip()]
        return manifest, entries

    # -------------------------------------------------------------- destino
    def _target(self, entry: dict[str, Any]) -> Path:
        key = entry["folder"]
        base = dest_base(key, self.dest_roots, is_file=key in self._file_keys)     # ValueError se fora do catálogo
        return long_path(resolve_in_root(base, entry["path"], lexical=True))

    def _space_path(self) -> Path:
        probe = self.dest_roots["profile"]
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return probe

    def _print_plan(self, label: str, entries: list[dict[str, Any]], todo: list[dict[str, Any]],
                    result: PassResult) -> None:
        by_key: dict[str, list[int]] = {}
        for e in todo:
            slot = by_key.setdefault(e["folder"], [0, 0])
            slot[0] += 1
            slot[1] += e["size"]
        self.log(f"\n[{label}] Plano")
        self.log(f"  Arquivos na origem:                  {len(entries)}")
        self.log(f"  Já presentes e iguais no destino:    {result.skipped}")
        self.log(f"  A copiar:                            {result.to_copy} "
                 f"({fmt_bytes(sum(e['size'] for e in todo))})")
        for key, (count, size) in sorted(by_key.items()):
            self.log(f"      {key:<20} {count:>6} arquivos {fmt_bytes(size):>10}")
        if result.failed:
            self.log(f"  Entradas inválidas ignoradas:        {len(result.failed)}")
