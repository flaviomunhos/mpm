"""Perfis de Wi-Fi (0.5.2): resumo e itens do menu. Sem acesso ao sistema aqui.

(Impressoras ficaram fora da primeira versão: dependem de driver do fabricante.)
Wi-Fi: o perfil (XML do `netsh wlan export`) leva a senha em claro enquanto trafega pelo canal TLS do pareamento;
o relatório e os arquivos gravados nunca a incluem.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

WLAN_NS = "{http://www.microsoft.com/networking/WLAN/profile/v1}"


def parse_wifi_xml(text: str) -> dict[str, Any]:
    """Resumo (sem a senha) de um perfil do `netsh wlan export`. Levanta ValueError se o XML for estranho."""
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ValueError("XML com declaração de entidades não é aceito")
    try:
        root = ET.fromstring(text.strip().encode("utf-8"))
    except ET.ParseError as exc:
        raise ValueError(f"XML inválido: {exc}") from exc

    def find(path: str) -> ET.Element | None:
        return root.find(path.replace("{}", WLAN_NS))

    name = find("{}name")
    if name is None or not (name.text or "").strip():
        raise ValueError("perfil sem nome")
    auth = find("{}MSM/{}security/{}authEncryption/{}authentication")
    cipher = find("{}MSM/{}security/{}authEncryption/{}encryption")
    mode = find("{}connectionMode")
    key = find("{}MSM/{}security/{}sharedKey/{}keyMaterial")
    security = find("{}MSM/{}security")
    onex = security is not None and any(el.tag.endswith("OneX") for el in security.iter())   # namespace próprio
    auth_text = (auth.text or "") if auth is not None else ""
    return {"name": name.text.strip(), "auth": auth_text, "encryption": (cipher.text or "") if cipher is not None else "",
            "mode": (mode.text or "") if mode is not None else "",
            "has_key": key is not None and bool((key.text or "").strip()),
            "enterprise": onex or auth_text.upper() in ("WPA", "WPA2", "WPA3ENT", "WPA3ENT192"),
            "open": auth_text.lower() == "open"}


def wifi_entries(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = []
    for w in profiles:
        if w["enterprise"]:
            note, selected = "802.1X (empresa): o perfil vai, mas pode pedir usuário/certificado ao conectar", True
        elif w["open"]:
            note, selected = "rede aberta", True
        elif w["has_key"]:
            note, selected = f"{w['auth']}; a senha será levada (trafega cifrada, não é gravada em relatório)", True
        else:
            note, selected = "senha NÃO lida (abra o TX como administrador): sem ela o perfil não conecta", False
        entries.append({"id": f"Wi-Fi: {w['name']}", "label": f"wifi:{w['name']}", "version": w["auth"],
                        "kind": "config", "selected": selected, "note": note})
    return entries


def render_devices(wifi: list[dict[str, Any]], elevated: bool | None = None) -> str:
    lines = ["WI-FI SALVO NO TX", f"  Perfis de Wi-Fi: {len(wifi)}"]
    for w in wifi:
        lines.append(f"  → Wi-Fi {w['name']:<35} {w['auth'] or '?':<14} "
                     + ("senha lida" if w["has_key"] else ("rede aberta" if w["open"] else "senha NÃO lida")))
    if elevated is False and wifi and not all(w["has_key"] or w["open"] or w["enterprise"] for w in wifi):
        lines.append("  Atenção: o TX não está como administrador, por isso as senhas do Wi-Fi não foram lidas.")
    return "\n".join(lines) + "\n"
