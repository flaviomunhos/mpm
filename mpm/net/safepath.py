"""Resolução segura de caminhos relativos vindos da rede.

Usada nos dois lados: o TX só serve arquivos dentro das pastas do perfil e o
RX só grava dentro do destino, mesmo que o outro lado envie caminhos hostis.
"""

from __future__ import annotations

import os
from pathlib import Path


def _is_link(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", None)      # Python 3.12+
    return os.path.islink(path) or bool(isjunction and isjunction(path))


def resolve_in_root(root: Path, rel: str, *, lexical: bool = False) -> Path:
    """Junta `rel` (separado por '/') a `root`; recusa qualquer escape da raiz.

    lexical=True (usado pelo RX, que grava): não usa `realpath`. Várias threads criam pastas ao
    mesmo tempo e, no Windows, `realpath` num caminho em criação (ou sob o antivírus) pode devolver
    um resultado que não confere com o da raiz, recusando arquivos válidos de forma intermitente
    (0.3.3). A segurança vem da validação de cada parte (sem '..', separadores ou ':') e da
    recusa de qualquer componente que seja link ou junction.
    """
    if not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")):
        raise ValueError("caminho inválido")
    parts = rel.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ValueError("caminho inválido")
        if os.name == "nt" and ("\\" in part or ":" in part):
            raise ValueError("caminho inválido")
        if "\x00" in part:
            raise ValueError("caminho inválido")

    candidate = root.joinpath(*parts)
    if lexical:
        for i in range(len(parts)):
            if _is_link(root.joinpath(*parts[: i + 1])):
                raise ValueError("links não são seguidos")
        return candidate
    real_root = os.path.realpath(root)
    real_candidate = os.path.realpath(candidate)
    try:
        inside = os.path.commonpath([real_root, real_candidate]) == real_root
    except ValueError:      # unidades diferentes no Windows
        inside = False
    if not inside:
        raise ValueError("caminho fora da pasta permitida")
    if os.path.islink(candidate):
        raise ValueError("links não são servidos")
    return candidate
