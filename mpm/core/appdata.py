"""Dados e configurações por aplicativo (AppData) — 0.5.0: só inventário, nada é copiado.

Cada *plugin* diz onde um programa guarda o que o usuário configurou (pastas em %APPDATA%, %LOCALAPPDATA%,
Documentos ou na raiz do perfil, e chaves do registro em HKCU), quais subpastas são só cache e o que
avisar. `scan` mede o que existe no TX; a cópia entra na 0.5.1 usando o mesmo catálogo.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mpm.core import outlook

ROOTS = ("appdata", "localappdata", "documents", "profile")


@dataclass(frozen=True)
class Plugin:
    id: str
    name: str
    paths: tuple[tuple[str, str], ...] = ()          # (raiz, caminho relativo com "/")
    registry: tuple[str, ...] = ()                   # chaves em HKCU
    skip_dirs: tuple[str, ...] = ()                  # nomes de subpasta que são só cache
    warning: str = ""
    group: str = "programa"


CHROMIUM_SKIP = (
    "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache", "DawnCache", "DawnGraphiteCache",
    "DawnWebGPUCache", "Service Worker", "CacheStorage", "ScriptCache", "Crashpad", "component_crx_cache",
    "extensions_crx_cache", "Safe Browsing", "optimization_guide_model_store", "BrowserMetrics", "Media Cache")
CHROMIUM_WARNING = ("senhas e cookies ficam cifrados com a conta do Windows (DPAPI) e NÃO funcionam em outro "
                    "usuário ou máquina: entre na conta do navegador (sincronização) no RX")

PLUGINS: tuple[Plugin, ...] = (
    Plugin("chrome", "Google Chrome", (("localappdata", "Google/Chrome/User Data"),),
           skip_dirs=CHROMIUM_SKIP, warning=CHROMIUM_WARNING, group="navegador"),
    Plugin("edge", "Microsoft Edge", (("localappdata", "Microsoft/Edge/User Data"),),
           skip_dirs=CHROMIUM_SKIP, warning=CHROMIUM_WARNING, group="navegador"),
    Plugin("brave", "Brave", (("localappdata", "BraveSoftware/Brave-Browser/User Data"),),
           skip_dirs=CHROMIUM_SKIP, warning=CHROMIUM_WARNING, group="navegador"),
    Plugin("firefox", "Mozilla Firefox", (("appdata", "Mozilla/Firefox"),), group="navegador",
           warning="perfil portátil: favoritos, extensões e senhas migram (senhas só abrem se não houver senha mestra)"),
    Plugin("putty", "PuTTY (sessões e chaves de host)", registry=(r"Software\SimonTatham",)),
    Plugin("winscp", "WinSCP (sites salvos)", (("appdata", "WinSCP.ini"),), (r"Software\Martin Prikryl",),
           warning="senhas salvas só abrem com a mesma senha mestra; confira os sites no RX"),
    Plugin("notepadpp", "Notepad++", (("appdata", "Notepad++"),)),
    Plugin("sevenzip", "7-Zip", registry=(r"Software\7-Zip",)),
    Plugin("filezilla", "FileZilla", (("appdata", "FileZilla"),),
           warning="gerenciador de sites pode ter senhas em texto simples; trate o relatório como sensível"),
    Plugin("vlc", "VLC", (("appdata", "vlc"),)),
    Plugin("obs", "OBS Studio", (("appdata", "obs-studio"),), skip_dirs=("crashes", "logs", "profiler_data")),
    Plugin("vscode", "Visual Studio Code", (("appdata", "Code/User"),)),
    Plugin("cura", "UltiMaker Cura", (("appdata", "cura"),)),
    Plugin("prusaslicer", "PrusaSlicer", (("appdata", "PrusaSlicer"),)),
    Plugin("arduino", "Arduino IDE", (("appdata", "arduino-ide"), ("profile", ".arduinoIDE"))),
    Plugin("drawio", "draw.io", (("appdata", "draw.io"),), skip_dirs=("Cache", "Code Cache", "GPUCache")),
    Plugin("dbbrowser", "DB Browser for SQLite", (("appdata", "sqlitebrowser"),)),
    Plugin("openvpn", "OpenVPN (perfis .ovpn)", (("profile", "OpenVPN"),),
           warning="contém chaves privadas e certificados: trate com cuidado", group="rede"),
    Plugin("outlook", "Outlook (contas, perfil, assinaturas e arquivos .pst)",
           (("appdata", "Microsoft/Signatures"), ("appdata", "Microsoft/Outlook"), ("appdata", "Microsoft/Templates"),
            ("appdata", "Microsoft/Stationery"), ("appdata", "Microsoft/UProof"),
            ("localappdata", "Microsoft/Outlook/RoamCache"),
            ("documents", "Arquivos do Outlook"), ("documents", "Outlook Files")),
           registry=outlook.REGISTRY_KEYS, group="e-mail",
           warning="o perfil (contas, servidores, opções) vai pelo registro; as senhas NÃO (cifradas pelo Windows): "
                   "o Outlook pede a senha de cada conta uma vez. Feche o Outlook nos dois PCs e não o abra no "
                   "PC novo antes; .pst fora do perfil não são copiados"),
    Plugin("ssh", "Chaves e configuração SSH", (("profile", ".ssh"),), group="rede",
           warning="contém chaves privadas: copie só por canal seguro e confira as permissões no RX"),
    Plugin("git", "Configuração do Git", (("profile", ".gitconfig"),), group="rede"),
    Plugin("mapped-drives", "Unidades de rede mapeadas", registry=(r"Network",), group="rede",
           warning="o mapeamento volta, mas a senha da conexão precisa ser digitada de novo"),
)


def _is_link(path: str) -> bool:
    return os.path.islink(path) or (hasattr(os.path, "isjunction") and os.path.isjunction(path))


def _walk(top: Path, skip: tuple[str, ...]) -> tuple[int, int, int]:
    """(arquivos, bytes, erros) abaixo de `top`, sem entrar em links nem em pastas de cache."""
    if top.is_file():
        try:
            return 1, top.stat().st_size, 0
        except OSError:
            return 0, 0, 1
    skip_cf = {s.casefold() for s in skip}
    files = size = errors = 0

    def onerror(_exc: OSError) -> None:
        nonlocal errors
        errors += 1

    for here, dirs, names in os.walk(top, onerror=onerror):
        dirs[:] = [d for d in dirs if d.casefold() not in skip_cf and not _is_link(os.path.join(here, d))]
        for name in names:
            full = os.path.join(here, name)
            try:
                if _is_link(full):
                    continue
                size += os.lstat(full).st_size
                files += 1
            except OSError:
                errors += 1
    return files, size, errors


def scan(
    plugins: tuple[Plugin, ...] | list[Plugin],
    roots: Mapping[str, Path],
    registry_exists: Callable[[str], bool] = lambda _key: False,
) -> list[dict[str, Any]]:
    """Mede, para cada plugin, o que existe nesta máquina. Somente leitura."""
    results: list[dict[str, Any]] = []
    for plugin in plugins:
        item: dict[str, Any] = {"id": plugin.id, "name": plugin.name, "group": plugin.group,
                                "warning": plugin.warning, "files": 0, "bytes": 0, "errors": 0,
                                "paths": [], "registry": []}
        for root, rel in plugin.paths:
            base = roots.get(root)
            path = base / Path(*rel.split("/")) if base is not None else None
            if path is None or not path.exists():
                continue
            files, size, errors = _walk(path, plugin.skip_dirs)
            item["paths"].append({"root": root, "rel": rel, "files": files, "bytes": size})
            item["files"] += files
            item["bytes"] += size
            item["errors"] += errors
        for key in plugin.registry:
            try:
                present = bool(registry_exists(key))
            except OSError:
                present = False
            if present:
                item["registry"].append(key)
        item["found"] = bool(item["files"] or item["registry"])
        results.append(item)
    return results


def _size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def render_text(results: list[dict[str, Any]], profile: str = "") -> str:
    found = [r for r in results if r["found"]]
    missing = [r for r in results if not r["found"]]
    total = sum(r["bytes"] for r in found)
    lines = ["DADOS E CONFIGURAÇÕES DOS PROGRAMAS NO TX" + (f"  (perfil lido: {profile})" if profile else ""),
             f"  Programas com dados: {len(found)} de {len(results)}   Tamanho total (sem cache): {_size(total)}", ""]
    for r in sorted(found, key=lambda r: -r["bytes"]):
        where = f"{r['files']} arq." if r["files"] else ""
        reg = f"{'+ ' if where else ''}registro" if r["registry"] else ""
        lines.append(f"  {r['name']:<42} {_size(r['bytes']):>10}  {where} {reg}".rstrip())
    warned = [r for r in found if r["warning"]]
    if warned:
        lines += ["", "AVISOS"]
        lines += [f"  {r['name']}: {r['warning']}" for r in warned]
    if any(r["errors"] for r in found):
        lines += ["", "  Alguns arquivos não puderam ser lidos (em uso ou sem permissão); a cópia fará nova tentativa."]
    if missing:
        lines += ["", "SEM DADOS NESTE PERFIL: " + ", ".join(r["name"] for r in missing)]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- cópia (0.5.1)

def plugin_by_id(plugin_id: str) -> Plugin:
    for plugin in PLUGINS:
        if plugin.id == plugin_id:
            return plugin
    raise ValueError(f"programa fora do catálogo: {plugin_id!r}")


def folder_key(plugin: Plugin, index: int) -> str:
    return f"{plugin.id}:{index}"


def parse_key(key: str) -> tuple[Plugin, int]:
    """'chrome:0' -> (plugin chrome, índice 0 em plugin.paths). Levanta ValueError se não for do catálogo."""
    plugin_id, _, number = str(key).partition(":")
    plugin = plugin_by_id(plugin_id)
    if not number.isdigit() or int(number) >= len(plugin.paths):
        raise ValueError(f"pasta fora do catálogo: {key!r}")
    return plugin, int(number)


def registry_allowlist(plugin_ids: list[str] | None = None) -> set[str]:
    """Chaves HKCU que o TX aceita exportar: só as do catálogo."""
    chosen = [p for p in PLUGINS if plugin_ids is None or p.id in plugin_ids]
    return {key for p in chosen for key in p.registry}


def inventory(plugin_ids: list[str], roots: Mapping[str, Path]) -> tuple[list[dict[str, Any]], dict[str, Path], dict[str, Any]]:
    """Inventário arquivo a arquivo das pastas dos plugins escolhidos (no TX).

    Retorna (entradas {folder, path, size, mtime_ns}, {chave: pasta-raiz}, {chave: {"file": bool}}).
    Uma pasta do catálogo que é um arquivo único (WinSCP.ini) vira raiz = pasta pai, caminho = nome do arquivo.
    Cache, links e junções ficam de fora."""
    entries: list[dict[str, Any]] = []
    folder_roots: dict[str, Path] = {}
    info: dict[str, Any] = {}
    for plugin_id in dict.fromkeys(plugin_ids):
        plugin = plugin_by_id(plugin_id)
        skip = {s.casefold() for s in plugin.skip_dirs}
        for index, (root, rel) in enumerate(plugin.paths):
            base = roots.get(root)
            if base is None:
                continue
            path = base / Path(*rel.split("/"))
            key = folder_key(plugin, index)
            if path.is_file() and not _is_link(str(path)):
                st = path.stat()
                folder_roots[key], info[key] = path.parent, {"file": True}
                entries.append({"folder": key, "path": path.name, "size": st.st_size, "mtime_ns": st.st_mtime_ns})
            elif path.is_dir() and not _is_link(str(path)):
                folder_roots[key], info[key] = path, {"file": False}
                for here, dirs, names in os.walk(path):
                    dirs[:] = [d for d in dirs if d.casefold() not in skip and not _is_link(os.path.join(here, d))]
                    for name in names:
                        full = os.path.join(here, name)
                        try:
                            if _is_link(full):
                                continue
                            st = os.lstat(full)
                        except OSError:
                            continue
                        entries.append({"folder": key, "path": Path(full).relative_to(path).as_posix(),
                                        "size": st.st_size, "mtime_ns": st.st_mtime_ns})
    return entries, folder_roots, info


def dest_base(key: str, dest_roots: Mapping[str, Path], *, is_file: bool) -> Path:
    """Pasta de destino (no RX) que corresponde à chave. Arquivo único -> a pasta que o contém."""
    plugin, index = parse_key(key)
    root, rel = plugin.paths[index]
    parts = rel.split("/")
    return dest_roots[root] / Path(*(parts[:-1] if is_file else parts))


def new_migration_id() -> str:
    return f"MPM-SET-{uuid.uuid4().hex[:10].upper()}"


_HKCU_HEADER = re.compile(r"^(\[-?)HKEY_CURRENT_USER(?=[\\\]])", re.I | re.M)


def retarget_reg(text: str, hive_name: str) -> str:
    """Aponta um .reg exportado do HKEY_CURRENT_USER para o hive carregado em HKEY_USERS (cabeçalhos de chave)."""
    return _HKCU_HEADER.sub(lambda m: m.group(1) + "HKEY_USERS" + chr(92) + hive_name, text)


__all__ = ["PLUGINS", "Plugin", "ROOTS", "render_text", "scan"]
