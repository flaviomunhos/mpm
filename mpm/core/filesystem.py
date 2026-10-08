"""Filesystem Discovery.

- As pastas copiadas (Desktop, Documents, Pictures): varredura completa,
  com inventário arquivo a arquivo (JSONL) opcional.
- AppData: apenas resumo por aplicativo (tamanho/contagem). A estratégia de
  migração do AppData é decidida em outro módulo (plugins/USMT).

Nunca segue links simbólicos nem junctions (o perfil do Windows tem várias,
como "Application Data", que causariam laços infinitos e cópias duplicadas).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from mpm import platforms
from mpm.core.models import (
    DEFAULT_FOLDER_NAMES,
    KNOWN_FOLDER_KEYS,
    FolderScan,
    KnownFolder,
    ProfileInfo,
)

# Atributos do Windows: RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE
_CLOUD_PLACEHOLDER_MASK = 0x400000 | 0x40000 | 0x1000
_MAX_SAMPLES = 100

RecordFn = Callable[[str, int, int], None]   # (caminho_relativo, tamanho, mtime_ns)


class InventoryWriter:
    """Grava o inventário por arquivo em JSONL (uma linha por arquivo)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: TextIO | None = self.path.open("w", encoding="utf-8", newline="\n")
        self.count = 0

    def add(self, folder: str, rel: str, size: int, mtime_ns: int) -> None:
        assert self._fh is not None
        self._fh.write(json.dumps(
            {"folder": folder, "path": rel, "size": size, "mtime_ns": mtime_ns},
            ensure_ascii=True,   # nomes inválidos em UTF-8 viram \udcXX, sem perder dados
        ) + "\n")
        self.count += 1

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "InventoryWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _note(samples: list[dict[str, str]], path: str, reason: str) -> None:
    if len(samples) < _MAX_SAMPLES:
        samples.append({"path": path, "reason": reason})


def scan_tree(root: Path, scan: FolderScan, record: RecordFn | None = None) -> FolderScan:
    """Percorre `root` e preenche os totais em `scan`."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError as exc:
            scan.error_count += 1
            _note(scan.errors, _rel(current, root), type(exc).__name__)
            continue
        try:
            with iterator:
                for entry in iterator:
                    try:
                        if entry.is_symlink() or entry.is_junction():
                            scan.skipped_count += 1
                            _note(scan.skipped, _rel(entry.path, root), "link/junction")
                        elif entry.is_dir(follow_symlinks=False):
                            scan.dir_count += 1
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            scan.file_count += 1
                            scan.total_bytes += st.st_size
                            if getattr(st, "st_file_attributes", 0) & _CLOUD_PLACEHOLDER_MASK:
                                scan.cloud_placeholders += 1
                            if record:
                                record(_rel(entry.path, root), st.st_size, st.st_mtime_ns)
                    except OSError as exc:
                        scan.error_count += 1
                        _note(scan.errors, _rel(entry.path, root), type(exc).__name__)
        except OSError as exc:
            scan.error_count += 1
            _note(scan.errors, _rel(current, root), type(exc).__name__)
    return scan


def _rel(path: str | Path, root: Path) -> str:
    try:
        return Path(path).relative_to(root).as_posix() or "."
    except ValueError:
        return str(path)


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def discover_folders(
    profile: ProfileInfo,
    writer: InventoryWriter | None = None,
) -> dict[str, FolderScan]:
    """Varre as pastas do perfil que serão copiadas."""
    resolved = platforms.current().known_folders(profile)
    result: dict[str, FolderScan] = {}
    for key in KNOWN_FOLDER_KEYS:
        folder = resolved.get(key) or KnownFolder(
            key, str(Path(profile.path) / DEFAULT_FOLDER_NAMES[key]), "default"
        )
        path = Path(folder.path)
        default = str(Path(profile.path) / DEFAULT_FOLDER_NAMES[key])
        scan = FolderScan(
            key=key,
            path=str(path),
            exists=path.is_dir(),
            resolved_by=folder.resolved_by,
            redirected=not _same_path(str(path), default),
        )
        if scan.exists:
            record: RecordFn | None = None
            if writer is not None:
                record = lambda rel, size, mtime, k=key: writer.add(k, rel, size, mtime)  # noqa: E731
            scan_tree(path, scan, record)
        result[key] = scan
    return result


def summarize_children(root: Path) -> dict[str, Any]:
    """Resumo por subpasta de primeiro nível (ex.: AppData\\Roaming\\<aplicativo>)."""
    summary: dict[str, Any] = {
        "path": str(root),
        "exists": root.is_dir(),
        "entries": [],
        "loose_files": {"file_count": 0, "total_bytes": 0},
    }
    if not summary["exists"]:
        return summary

    entries: list[dict[str, Any]] = []
    try:
        children = sorted(os.scandir(root), key=lambda e: e.name.lower())
    except OSError as exc:
        summary["error"] = type(exc).__name__
        return summary

    for entry in children:
        try:
            if entry.is_symlink() or entry.is_junction():
                continue
            if entry.is_dir(follow_symlinks=False):
                scan = scan_tree(
                    Path(entry.path),
                    FolderScan(key=entry.name, path=entry.path, exists=True,
                               resolved_by="scan", redirected=False),
                )
                entries.append({
                    "name": entry.name,
                    "file_count": scan.file_count,
                    "dir_count": scan.dir_count,
                    "total_bytes": scan.total_bytes,
                    "error_count": scan.error_count,
                })
            elif entry.is_file(follow_symlinks=False):
                summary["loose_files"]["file_count"] += 1
                summary["loose_files"]["total_bytes"] += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue

    entries.sort(key=lambda e: e["total_bytes"], reverse=True)
    summary["entries"] = entries
    return summary


def discover_appdata(profile: ProfileInfo) -> dict[str, Any]:
    """Resumo do AppData (Roaming / Local / LocalLow). Vazio fora do Windows."""
    return {
        name: summarize_children(root)
        for name, root in platforms.current().appdata_roots(profile).items()
    }
