"""Inventário de aplicativos (0.4.0): separa o que vale reinstalar do ruído do Windows.

Entradas (vêm do TX):
  registry  lista de programas lidos do registro (nome, versão, fabricante, escopo...)
  export    JSON do `winget export` (só o que o winget reconhece, com ID de pacote)

Saída: grupos para o relatório e um JSON pronto para o `winget import`.

  winget     tem ID de pacote e não é componente do sistema nem driver: instalável (padrão: marcado)
  system     runtimes e componentes do Windows (VC++, .NET, WindowsAppRuntime...): vêm com outros programas
  oem        drivers e utilitários do fabricante do PC antigo: não servem na máquina nova
  manual     exige licença, política ou instalador próprio (Office, antivírus corporativo...)
  unmatched  programa de verdade sem pacote conhecido no winget: instalar à mão (ou resolver em 0.4.1)
"""

from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any

SYSTEM_NAME = re.compile(
    r"^(microsoft (visual c\+\+|\.net|asp\.net|windows desktop|edge|update health|activex|capicom|"
    r"visual studio|teams machine|teams meeting add-in|vclibs)|msxml|microsoft\.ui\.xaml|windowsappruntime|"
    r"windows (software development kit|sdk)|teams machine-wide|python launcher|mozilla maintenance|"
    r"tap-windows|copilot$)|\bassistente\s+de\s+.*windows|"
    r"\b(update for|security update|hotfix)\b|\bkb\d{6,}\b", re.I)
SYSTEM_ID = re.compile(
    r"^(Microsoft\.(VCRedist|VCLibs|DotNet|UI\.Xaml|WindowsAppRuntime|AppInstaller|Edge($|\.)|"
    r"UpdateAssistant|WindowsInstallationAssistant|Teams\.Classic|OneDrive|EdgeWebView2|365Copilot)|"
    r"Python\.Launcher$)", re.I)
OEM_NAME = re.compile(
    r"^(intel\(r\)|intel®|realtek|qualcomm|dell |samsung[ _](usb|monsetup)|fresco logic|"
    r"pacote de driver|quickset|aic8800|uninstall usm)|\bdriver\b|\bfirmware\b", re.I)
OEM_ID = re.compile(r"^(Dell\.|Lenovo\.|HP\.|Intel\.|Realtek|Qualcomm|Samsung\.)", re.I)
MANUAL_NAME = {
    r"mcafee": "agente de antivírus gerenciado: reinstale pela política da empresa",
    r"warsaw": "plugin bancário: baixe do site do banco",
    r"microsoft 365 apps|^microsoft onenote - |^microsoft office": "Office: reinstalar e ativar com a conta/licença",
}

_STRIP = re.compile(r"\([^)]*\)|\bv?\d+(\.\d+)+\S*|\bx(64|86)\b|\b(32|64)[- ]?bits?\b", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _STRIP.sub(" ", text).lower())


# Palavras genéricas que não identificam um programa ("VLC media player" = VLC).
_GENERIC = {"media", "player", "edition", "release", "update", "setup", "installer", "version",
            "amd64", "windows", "pro", "for", "the", "and", "msi", "bit", "bits", "x64", "x86", "win"}


# Instalar no RX só faz sentido com o dispositivo/serviço presente: vêm desmarcados.
DESELECT_ID = {
    r"^AETEurope\.SafeSign": "driver de token USB: instale só se o token for usado nesta máquina",
    r"^Apple\.Bonjour": "serviço de descoberta de rede, normalmente instalado por outro programa",
}

# Programas sem pacote no registro do winget, mas com ID conhecido (nome do registro -> ID).
# São só *sugestões*: o RX confirma que o ID existe (`winget show`) e o usuário aceita item a item.
CANDIDATES = [
    (r"^google chrome", "Google.Chrome"),
    (r"^mozilla firefox", "Mozilla.Firefox.pt-BR"),
    (r"^brave$", "Brave.Brave"),
    (r"^winscp", "WinSCP.WinSCP"),
    (r"^adobe acrobat", "Adobe.Acrobat.Reader.64-bit"),
    (r"^xampp", "ApacheFriends.Xampp.8.2"),
    (r"^python 3\.14", "Python.Python.3.14"),
    (r"^realvnc connect viewer|^vnc viewer", "RealVNC.VNCViewer"),
    (r"^ghostscript gpl", "ArtifexSoftware.GhostScript"),
    (r"^cisco packet tracer", "Cisco.PacketTracer"),
]


def _tokens(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", _STRIP.sub(" ", name).lower())
    return [w for w in words if len(w) >= 3 and not w.isdigit() and w not in _GENERIC]


def _covered(name: str, id_norms: list[str]) -> bool:
    """O programa do registro é o mesmo de algum pacote do winget? (todas as palavras-chave no ID)"""
    norm = _norm(name)
    if len(norm) >= 4 and any(norm in idn for idn in id_norms):
        return True
    tokens = _tokens(name)
    if not tokens or sum(len(t) for t in tokens) < 3:
        return False
    return any(all(t in idn for t in tokens) for idn in id_norms)


def parse_winget_export(data: dict[str, Any] | None) -> list[dict[str, str]]:
    """Lista plana {id, version, source} a partir do JSON do `winget export`."""
    packages: list[dict[str, str]] = []
    for source in (data or {}).get("Sources", []) or []:
        name = (source.get("SourceDetails") or {}).get("Name", "")
        for pkg in source.get("Packages", []) or []:
            pid = pkg.get("PackageIdentifier")
            if isinstance(pid, str) and pid:
                packages.append({"id": pid, "version": str(pkg.get("Version", "")), "source": name})
    return packages


def _is_component(app: dict[str, Any]) -> bool:
    return bool(app.get("system_component") or app.get("is_update") or not app.get("name"))


def build_report(
    registry: list[dict[str, Any]],
    export: dict[str, Any] | None,
    *,
    extra_ignore: list[str] | None = None,
) -> dict[str, Any]:
    extra = [re.compile(p, re.I) for p in (extra_ignore or [])]
    groups: dict[str, list[dict[str, Any]]] = {
        "winget": [], "system": [], "oem": [], "manual": [], "unmatched": []}

    for pkg in parse_winget_export(export):
        if SYSTEM_ID.search(pkg["id"]):
            groups["system"].append({**pkg, "name": pkg["id"]})
        elif OEM_ID.search(pkg["id"]):
            groups["oem"].append({**pkg, "name": pkg["id"]})
        else:
            reason = next((msg for pat, msg in DESELECT_ID.items() if re.search(pat, pkg["id"], re.I)), None)
            item = {**pkg, "name": pkg["id"], "selected": reason is None}
            if reason:
                item["note"] = reason
            groups["winget"].append(item)
    covered = [_norm(p["id"]) for p in groups["winget"]]

    components = duplicates = 0
    seen: set[tuple[str, str]] = set()
    for app in registry:
        if _is_component(app):
            components += 1
            continue
        name = " ".join(unicodedata.normalize("NFKC", str(app["name"])).split())   # NBSP, espaços nas pontas
        key = (name.lower(), str(app.get("version", "")))
        if key in seen:
            continue
        seen.add(key)
        if _covered(name, covered):
            duplicates += 1                   # o mesmo programa já aparece no grupo winget
            continue
        item = {"name": name, "version": app.get("version", ""), "publisher": app.get("publisher", ""),
                "scope": app.get("scope", "")}
        note = next((msg for pat, msg in MANUAL_NAME.items() if re.search(pat, name, re.I)), None)
        if any(p.search(name) for p in extra) or SYSTEM_NAME.search(name):
            groups["system"].append(item)
        elif OEM_NAME.search(name):
            groups["oem"].append(item)
        elif note:
            groups["manual"].append({**item, "note": note})
        else:
            guess = next((pid for pat, pid in CANDIDATES if re.search(pat, name, re.I)), None)
            if guess:
                item["candidate"] = guess
            groups["unmatched"].append(item)

    for items in groups.values():
        items.sort(key=lambda i: str(i["name"]).lower())
    return {
        "counts": {**{k: len(v) for k, v in groups.items()},
                   "registry_total": len(registry), "components_hidden": components,
                   "covered_by_winget": duplicates},
        "groups": groups,
    }


def import_json(export: dict[str, Any] | None, selected_ids: set[str]) -> dict[str, Any]:
    """`winget export` filtrado: só os pacotes escolhidos, pronto para `winget import`."""
    out = copy.deepcopy(export or {})
    sources = []
    for source in out.get("Sources", []) or []:
        source["Packages"] = [p for p in source.get("Packages", []) if p.get("PackageIdentifier") in selected_ids]
        if source["Packages"]:
            sources.append(source)
    out["Sources"] = sources
    return out


def render_text(report: dict[str, Any], *, show_all: bool = False) -> str:
    g, c = report["groups"], report["counts"]
    lines = [
        "INVENTÁRIO DE APLICATIVOS DO TX",
        f"  Programas no registro: {c['registry_total']} (componentes/atualizações ocultos: {c['components_hidden']})",
        f"  Instaláveis pelo winget: {c['winget']}   Sem pacote: {c['unmatched']}   "
        f"Manuais: {c['manual']}   Sistema: {c['system']}   Drivers/OEM: {c['oem']}",
        "",
        f"[1] INSTALÁVEIS PELO WINGET ({c['winget']}) — entram no winget-import.json",
    ]
    lines += [f"    {i['id']:<42} {i['version']}" + ("" if i.get("selected", True) else f"  [desmarcado: {i['note']}]")
              for i in g["winget"]]
    lines += ["", f"[2] SEM PACOTE NO WINGET ({c['unmatched']}) — instalar à mão; com `→` há um ID provável a confirmar"]
    lines += [f"    {i['name']:<46} {i['version']}  {i.get('publisher', '')}"
              + (f"  → {i['candidate']}" if i.get("candidate") else "") for i in g["unmatched"]]
    lines += ["", f"[3] MANUAIS ({c['manual']}) — licença, política ou instalador próprio"]
    lines += [f"    {i['name']:<46} {i['version']}  → {i['note']}" for i in g["manual"]]
    for key, title in (("oem", "DRIVERS E UTILITÁRIOS DO FABRICANTE"), ("system", "SISTEMA E RUNTIMES")):
        lines += ["", f"[ ] {title} ({c[key]}) — ignorados"
                  + ("" if show_all else " (use --all para listar)")]
        if show_all:
            lines += [f"    {i['name']:<46} {i.get('version', '')}" for i in g[key]]
    return "\n".join(lines) + "\n"
