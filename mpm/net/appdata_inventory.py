"""RX: pede ao TX o inventário de dados dos programas e grava o relatório (nada é copiado)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mpm.core.appdata import render_text
from mpm.net.rx import RxClient


class ProfileError(Exception):
    """Perfil de origem inválido ou ambíguo."""


def pick_source_profile(client: RxClient, wanted: str | None,
                        choose: Callable[[list[dict[str, Any]]], str] | None = None) -> str:
    """Escolhe o perfil do TX de onde ler as configurações (mesma regra da cópia do perfil)."""
    profiles: list[dict[str, Any]] = client.request({"op": "info"})["profiles"]
    names = ", ".join(p["username"] for p in profiles) or "(nenhum)"
    if wanted:
        for p in profiles:
            if wanted.lower() in (p["username"].lower(), p["sid"].lower()):
                return p["username"]
        raise ProfileError(f"perfil {wanted!r} não existe no TX. Disponíveis: {names}")
    if choose and len(profiles) > 1:
        return choose(profiles)
    for p in profiles:
        if p.get("is_current"):
            return p["username"]
    if len(profiles) == 1:
        return profiles[0]["username"]
    raise ProfileError(f"indique o perfil com --source-user. Disponíveis: {names}")


def run_appdata_inventory(client: RxClient, outdir: Path, *, user: str | None = None,
                          log: Callable[[str], None] = print) -> list[dict[str, Any]]:
    log("\nPedindo ao TX o inventário dos dados e configurações dos programas (pode levar alguns minutos) ...")
    reply = client.request({"op": "scan_appdata", **({"user": user} if user else {})})
    for error in reply.get("errors", []):
        log(f"  Aviso do TX: {error}")
    results = reply.get("results", [])
    profile = reply.get("profile", "")
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "appdata-inventory.json").write_text(
        json.dumps({"profile": profile, "results": results}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    text = render_text(results, profile)
    (outdir / "appdata-report.txt").write_text(text, encoding="utf-8")
    log(text)
    log(f"Arquivos gravados em {outdir}:\n  appdata-report.txt     relatório\n  appdata-inventory.json dados brutos")
    return results
