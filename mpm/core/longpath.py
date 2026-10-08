"""Caminhos longos no Windows (limite clássico de 260 caracteres).

Perfis reais têm caminhos maiores que isso (extensões do Firefox, node_modules...). Com o prefixo
`\\\\?\\` o Windows aceita até ~32 mil caracteres. Só é aplicado quando o caminho já é longo e
só no Windows; nos demais sistemas devolve o mesmo caminho.
"""

from __future__ import annotations

import os
from pathlib import Path

LIMIT = 240          # abaixo do limite de 260 para sobrar espaço ao sufixo ".mpm-part"
PREFIX = "\\\\?\\"


def long_path(path: Path, *, force: bool = False, windows: bool | None = None) -> Path:
    is_windows = (os.name == "nt") if windows is None else windows
    if not is_windows:
        return path
    text = str(path)
    if text.startswith(PREFIX) or (len(text) < LIMIT and not force):
        return path
    text = os.path.abspath(text) if os.name == "nt" else text
    if text.startswith("\\\\"):
        return Path(PREFIX + "UNC\\" + text[2:])
    return Path(PREFIX + text)
