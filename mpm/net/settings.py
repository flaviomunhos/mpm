"""RX: copiar as configurações dos programas do TX para um usuário do RX (0.5.1).

Fluxo: inventário do TX -> menu (Express/Custom) -> arquivos (motor verificado) -> registro (PuTTY, WinSCP,
7-Zip, unidades de rede) -> propriedade dos arquivos. Nada disso instala programas: os programas vêm de
`--apps --install`; aqui só vão as configurações deles.
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, TextIO

from mpm.core.appdata import _size, plugin_by_id
from mpm.core.devices import parse_wifi_xml, render_devices, wifi_entries
from mpm.net import apps_install
from mpm.net.appdata_inventory import ProfileError, pick_source_profile, run_appdata_inventory
from mpm.net.migration import MigrationError, Options
from mpm.net.rx import RemoteError, RxClient
from mpm.net.settings_migration import SettingsMigration


class SettingsError(Exception):
    """Erro que impede a cópia das configurações."""


@dataclass
class Backends:
    accounts: ModuleType
    appdata: ModuleType
    regtools: ModuleType
    devices: ModuleType | None = None


def resolve_destination(to_user: str | None, backends: Backends) -> tuple[dict[str, Path], Path | None]:
    """(pastas do usuário de destino, perfil para carregar o hive; None = usuário atual)."""
    if to_user:
        profile = backends.accounts.existing_profile_path(to_user)
        if profile is None:
            raise SettingsError(
                f"o usuário {to_user!r} não existe ou ainda não tem perfil. Crie-o com `rx --as-user {to_user}` "
                "(ou entre nele uma vez) e tente de novo.")
        roots = {"profile": profile, "appdata": profile / "AppData" / "Roaming",
                 "localappdata": profile / "AppData" / "Local", "documents": profile / "Documents"}
        return roots, profile
    return dict(backends.appdata.roots()), None


def settings_entries(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Itens do menu: um por programa que tem dados no TX."""
    entries = []
    for r in sorted((r for r in results if r["found"]), key=lambda r: r["name"].lower()):
        what = _size(r["bytes"]) if r["files"] else "registro"
        if r["files"] and r["registry"]:
            what += " + registro"
        entries.append({"id": r["name"], "label": r["id"], "version": what, "kind": "config",
                        "selected": True, "note": r["warning"]})
    return entries


def _choose(entries: list[dict[str, Any]], mode: str | None, only: str | None, assume_yes: bool,
            ask: Callable[[str], str], log: Callable[[str], None]) -> tuple[str | None, list[dict[str, Any]] | None]:
    if mode == "menu":
        mode = "express" if assume_yes else apps_install.ask_mode(
            ask=ask, log=log, title="CONFIGURAÇÕES DOS PROGRAMAS", verb="copia", cancel="Não copiar agora")
    if only is not None:
        entries = apps_install.only(entries, only)
        if not any(e["selected"] for e in entries):
            log(f"Nenhum item casa com --only {only!r}.")
            return mode, None
    if mode == "express":
        return mode, entries
    if mode == "custom":
        return mode, apps_install.choose(entries, ask=ask, log=log,
                                         title="MODO CUSTOM — escolha as configurações a copiar", action="copiar")
    return mode, None


def restore_registry(client: RxClient, keys: list[str], meta: Path, regtools: ModuleType,
                     hive_profile: Path | None, log: Callable[[str], None],
                     source_user: str | None = None) -> list[dict[str, str]]:
    """Exporta as chaves do TX e importa no usuário de destino (hive carregado) ou no usuário atual."""
    if not keys:
        return []
    reply = client.request({"op": "export_registry", "keys": keys, **({"user": source_user} if source_user else {})})
    for error in reply.get("errors", []):
        log(f"  Aviso do TX: {error}")
    exports: dict[str, str] = reply.get("exports", {})
    saved = meta / "registry"
    saved.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, str]] = []
    hive = f"MPM_{secrets.token_hex(4).upper()}" if hive_profile else None
    load_error = regtools.load_hive(hive, hive_profile / "NTUSER.DAT") if hive and hive_profile else None
    try:
        for number, key in enumerate(sorted(exports), 1):
            data = base64.b64decode(exports[key])
            (saved / f"{number:02d}.reg").write_bytes(data)
            if load_error:
                results.append({"key": key, "status": "falhou",
                                "detail": f"não consegui carregar o perfil do usuário ({load_error}); "
                                          "se ele estiver com sessão aberta, saia dela"})
                continue
            error = regtools.import_reg(data, hive)
            results.append({"key": key, "status": "importado" if error is None else "falhou", "detail": error or ""})
    finally:
        if hive and not load_error:
            unload_error = regtools.unload_hive(hive)
            if unload_error:
                log(f"  Aviso: não consegui descarregar o hive {hive} ({unload_error}); ele se solta ao reiniciar.")
    for r in results:
        log(f"  registro {r['key']:<34} → {r['status']}" + (f": {r['detail']}" if r["detail"] else ""))
    missing = [k for k in keys if k not in exports]
    for key in missing:
        results.append({"key": key, "status": "ausente", "detail": "a chave não existe no usuário do TX"})
    return results


def scan_devices(client: RxClient, log: Callable[[str], None]) -> dict[str, Any]:
    """Wi-Fi salvo do TX. Falha vira aviso: os programas continuam."""
    try:
        reply = client.request({"op": "scan_devices"})
    except (RemoteError, OSError) as exc:
        log(f"  Aviso: não consegui ler o Wi-Fi do TX ({exc}).")
        return {"wifi": [], "elevated": None}
    for error in reply.get("errors", []):
        log(f"  Aviso do TX: {error}")
    log(render_devices(reply.get("wifi", []), reply.get("elevated")))
    return reply


def restore_wifi(client: RxClient, names: list[str], backend: ModuleType, *, dry_run: bool,
                 log: Callable[[str], None]) -> list[dict[str, str]]:
    """Importa os perfis de Wi-Fi escolhidos (para todos os usuários). A senha nunca é gravada em relatório."""
    results: list[dict[str, str]] = []
    try:
        existing = backend.existing_wifi_names()
    except (RuntimeError, OSError) as exc:
        log(f"  Aviso: não consegui listar os perfis de Wi-Fi deste computador ({exc}).")
        existing = set()
    todo = [n for n in names if n.casefold() not in existing]
    for name in names:
        if name.casefold() in existing:
            results.append({"name": name, "status": "ja_existe", "detail": "já há um perfil com esse nome aqui"})
    profiles: dict[str, str] = {}
    if todo and not dry_run:
        reply = client.request({"op": "export_wifi", "names": todo})
        for error in reply.get("errors", []):
            log(f"  Aviso do TX: {error}")
        for p in reply.get("profiles", []):
            try:
                info = parse_wifi_xml(p["xml"])
            except (ValueError, KeyError) as exc:
                results.append({"name": str(p.get("name", "?")), "status": "falhou", "detail": f"perfil inválido ({exc})"})
                continue
            if info["name"].casefold() in {n.casefold() for n in todo}:
                profiles[info["name"].casefold()] = p["xml"]
    for name in todo:
        if dry_run:
            results.append({"name": name, "status": "simulado", "detail": "importaria o perfil"})
            continue
        xml = profiles.get(name.casefold())
        if xml is None:
            results.append({"name": name, "status": "falhou", "detail": "o TX não devolveu esse perfil"})
            continue
        try:
            error = backend.add_wifi_profile(xml)
        except (RuntimeError, OSError) as exc:
            error = str(exc)
        results.append({"name": name, "status": "importado" if error is None else "falhou", "detail": error or ""})
    for r in results:
        log(f"  Wi-Fi {r['name']:<40} → {r['status']}" + (f": {r['detail']}" if r["detail"] else ""))
    return results


def run_settings(
    client: RxClient,
    workers: list[RxClient],
    backends: Backends,
    *,
    outdir: Path,
    mode: str | None = "menu",
    only: str | None = None,
    to_user: str | None = None,
    source_user: str | None = None,
    choose_profile: Callable[[list[dict[str, Any]]], str] | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
    retries: int = 2,
    retry_wait: float = 2.0,
    ask: Callable[[str], str] = input,
    log: Callable[[str], None] = print,
    progress_stream: TextIO | None = None,
) -> dict[str, Any]:
    """Retorna o resumo (também gravado em <outdir>/settings/settings-report.json)."""
    dest_roots, hive_profile = resolve_destination(to_user, backends)
    log("Destino das configurações: " + (f"usuário {to_user} ({hive_profile})" if to_user
                                         else f"usuário atual ({dest_roots['profile']})"))
    try:
        source = pick_source_profile(client, source_user, choose_profile)
    except ProfileError as exc:
        raise SettingsError(str(exc)) from exc
    log(f"Perfil de origem no TX: {source}")
    results = run_appdata_inventory(client, outdir, user=source, log=log)
    devices = scan_devices(client, log) if backends.devices is not None else {"wifi": []}
    wifi_names = {f"wifi:{w['name']}": w["name"] for w in devices.get("wifi", [])}
    entries = settings_entries(results) + wifi_entries(devices.get("wifi", []))
    summary: dict[str, Any] = {"ok": True, "dry_run": dry_run, "to_user": to_user, "source_user": source, "programs": [],
                               "files": None, "registry": [], "wifi": []}
    if not entries:
        log("Nada para copiar: nenhum programa do catálogo ou rede Wi-Fi no TX.")
        return summary
    mode, chosen = _choose(entries, mode, only, assume_yes, ask, log)
    if chosen is None:
        log("Nada foi copiado.")
        summary["programs"] = []
        return summary
    picked = [e["label"] for e in chosen if e["selected"]]
    ids = [i for i in picked if ":" not in i]
    summary["programs"] = ids
    by_id = {r["id"]: r for r in results}
    with_files = [i for i in ids if by_id[i]["files"]]
    reg_keys = sorted({k for i in ids for k in by_id[i]["registry"]})
    log(f"\nCopiando: {len(ids)} programas, {sum(i in wifi_names for i in picked)} redes Wi-Fi ...")

    if with_files:
        migration = SettingsMigration(
            client, Options(dest=outdir, retries=retries, retry_wait=retry_wait, dry_run=dry_run,
                            assume_yes=assume_yes or mode == "express", busy_is_warning=True),
            plugin_ids=with_files, dest_roots=dest_roots, source_user=source, log=log, ask=lambda q: ask(q + " [S/n] ").strip().lower()
            not in ("n", "nao", "não"), workers=workers,
            progress_stream=progress_stream)
        try:
            summary["files"] = migration.run()
        except MigrationError as exc:
            raise SettingsError(str(exc)) from exc
        summary["ok"] = bool(summary["files"].get("ok", True))
    if reg_keys:
        if dry_run:
            log(f"\nRegistro: {len(reg_keys)} chaves seriam importadas (simulação).")
        else:
            log("\nImportando as chaves do registro ...")
            summary["registry"] = restore_registry(client, reg_keys, outdir / "settings", backends.regtools,
                                                   hive_profile, log, source)
            if any(r["status"] == "falhou" for r in summary["registry"]):
                summary["ok"] = False

    if "outlook" in ids and not dry_run and summary["registry"]:
        summary["outlook"] = _outlook_notes(outdir / "settings" / "registry", source, to_user, dest_roots, log)

    chosen_wifi = [wifi_names[i] for i in picked if i in wifi_names]
    if chosen_wifi and backends.devices is not None:
        log("\nImportando redes Wi-Fi ...")
        summary["wifi"] = restore_wifi(client, chosen_wifi, backends.devices, dry_run=dry_run, log=log)
        if any(r["status"] == "falhou" for r in summary["wifi"]):
            summary["ok"] = False

    if to_user and not dry_run:
        _hand_over(to_user, ids, dest_roots, backends, log)
    if not dry_run:
        out = outdir / "settings"
        out.mkdir(parents=True, exist_ok=True)
        (out / "settings-report.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        log(f"\nRelatório gravado em {out / 'settings-report.json'}")
    return summary


def _outlook_notes(saved: Path, source: str, to_user: str | None, dest_roots: dict[str, Path],
                   log: Callable[[str], None]) -> list[dict[str, str]]:
    """Avisos sobre os arquivos .pst citados no perfil do Outlook (somente leitura dos .reg gravados)."""
    from mpm.core import outlook
    dest_user = to_user or dest_roots["profile"].name
    files = outlook.report(saved, source, dest_user)
    notes = [{"path": f.path, "status": f.status, "note": f.note} for f in files]
    log("\nOutlook: as contas voltam sem senha (o Outlook pede a senha de cada uma na primeira vez).")
    for f in files:
        log(f"  {f.path}\n      {f.note}")
    if not files:
        log("  Nenhum arquivo .pst citado no perfil (contas só de servidor).")
    return notes


def _hand_over(user: str, plugin_ids: list[str], dest_roots: dict[str, Path], backends: Backends,
               log: Callable[[str], None]) -> None:
    """Passa a propriedade do que foi copiado ao usuário de destino (melhor esforço)."""
    done: set[Path] = set()
    for plugin_id in plugin_ids:
        plugin = plugin_by_id(plugin_id)
        for index, (root, rel) in enumerate(plugin.paths):
            target = dest_roots[root] / Path(*rel.split("/"))
            if target in done or not target.exists():
                continue
            done.add(target)
            problem = backends.accounts.set_owner(target, user)
            if problem:
                log(f"  Aviso: não consegui passar {target} para {user} ({problem})")
