"""Outlook clássico: o que o registro diz sobre os arquivos de dados (.pst/.ost) de cada conta.

O perfil do Outlook (contas, servidores, opções) fica em HKCU\\Software\\Microsoft\\Office\\<versão>\\Outlook\\Profiles
e é levado como chaves do registro. As senhas ficam cifradas com a conta do Windows (DPAPI) e não abrem em
outro usuário ou máquina: o Outlook pede a senha de cada conta uma vez, mas servidor, porta, nome e opções vêm
prontos. Os caminhos dos arquivos .pst aparecem dentro do perfil como texto UTF-16 em valores binários; aqui
eles são lidos (somente leitura) para avisar o que não acompanha a migração.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

OFFICE_VERSIONS = ("16.0", "15.0", "14.0")
REGISTRY_KEYS = tuple(rf"Software\Microsoft\Office\{v}\Outlook" for v in OFFICE_VERSIONS) + (
    r"Software\Microsoft\Windows NT\CurrentVersion\Windows Messaging Subsystem\Profiles",)

_PATH = re.compile(r"[A-Za-z]:\\[^\x00\r\n\"<>|*?]{1,240}?\.(?:pst|ost)\b|\\\\[^\x00\r\n\"<>|*?]{1,240}?\.(?:pst|ost)\b",
                   re.I)


@dataclass(frozen=True)
class DataFile:
    path: str
    status: str      # "no-perfil" | "outro-usuario" | "fora-do-perfil" | "ost"
    note: str


def _hex_values(text: str) -> list[bytes]:
    """Valores binários (`hex:`, `hex(7):` ...) de um .reg, já sem as quebras de linha com `\\`."""
    joined = re.sub(r"\\\r?\n\s*", "", text)
    out = []
    for m in re.finditer(r"=hex(?:\([0-9a-fA-F]+\))?:([0-9a-fA-F,\s]*)", joined):
        digits = re.sub(r"[^0-9a-fA-F]", "", m.group(1))
        if len(digits) % 2 == 0 and digits:
            out.append(bytes.fromhex(digits))
    return out


def find_data_files(reg: bytes) -> list[str]:
    """Caminhos .pst/.ost citados num .reg exportado (UTF-16), sem repetição, na ordem em que aparecem."""
    try:
        text = reg.decode("utf-16")
    except UnicodeDecodeError:
        return []
    found: dict[str, None] = {}
    blobs = _hex_values(text)
    chunks = [b.decode("utf-16-le", errors="ignore") for b in blobs]
    chunks += [b[1:].decode("utf-16-le", errors="ignore") for b in blobs]      # texto desalinhado em 1 byte
    chunks.append(text.replace("\\\\", "\\"))
    for chunk in chunks:
        for m in _PATH.finditer(chunk):
            found.setdefault(m.group(0), None)
    return list(found)


def classify(paths: list[str], source_user: str, dest_user: str) -> list[DataFile]:
    """Diz, para cada arquivo de dados, se ele acompanha o perfil migrado."""
    result = []
    marker = f"\\users\\{source_user}\\".casefold()
    for p in paths:
        low = p.casefold()
        if low.endswith(".ost"):
            result.append(DataFile(p, "ost", "cache do servidor; o Outlook baixa de novo (não é copiado)"))
        elif marker in low:
            if source_user.casefold() == dest_user.casefold():
                result.append(DataFile(p, "no-perfil", "vai junto com o perfil"))
            else:
                result.append(DataFile(p, "outro-usuario",
                                       f"vai junto com o perfil, mas em C:\\Users\\{dest_user}; no Outlook use "
                                       "'Procurar' quando ele pedir o arquivo"))
        else:
            result.append(DataFile(p, "fora-do-perfil", "está FORA do perfil: copie à mão para o PC novo"))
    return result


def report(saved: Path, source_user: str, dest_user: str) -> list[DataFile]:
    """Lê os .reg gravados pela migração (<saved>/*.reg) e classifica os arquivos de dados do Outlook."""
    paths: dict[str, None] = {}
    for reg in sorted(saved.glob("*.reg")):
        for p in find_data_files(reg.read_bytes()):
            paths.setdefault(p, None)
    return classify(list(paths), source_user, dest_user)
