"""RX: pede o inventário de aplicativos ao TX e grava o relatório (nada é instalado)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mpm.core.apps import build_report, import_json, parse_winget_export, render_text
from mpm.net.rx import RxClient


def run_apps_inventory(
    client: RxClient,
    outdir: Path,
    *,
    show_all: bool = False,
    extra_ignore: list[str] | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    log("\nPedindo ao TX a lista de aplicativos instalados (pode levar 1 a 2 minutos) ...")
    reply = client.request({"op": "list_apps", "winget": True})
    for error in reply.get("errors", []):
        log(f"  Aviso do TX: {error}")

    registry = reply.get("apps", [])
    export = reply.get("winget")
    report = build_report(registry, export, extra_ignore=extra_ignore)
    selected = {p["id"] for p in report["groups"]["winget"] if p.get("selected")}

    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "apps-inventory.json").write_text(
        json.dumps({"registry": registry, "winget_export": export, "report": report},
                   indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    text = render_text(report, show_all=True)
    (outdir / "apps-report.txt").write_text(text, encoding="utf-8")
    (outdir / "winget-import.json").write_text(
        json.dumps(import_json(export, selected), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    log(render_text(report, show_all=show_all))
    log(f"Arquivos gravados em {outdir}:\n"
        "  apps-report.txt        relatório completo\n"
        "  apps-inventory.json    dados brutos e classificação\n"
        "  winget-import.json     pacotes instaláveis (para `winget import`, na 0.4.1)")
    if not parse_winget_export(export):
        log("Atenção: o TX não devolveu pacotes do winget; o relatório usa só o registro.")
    return report
