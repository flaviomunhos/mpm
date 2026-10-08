"""Contas de usuário no destino (RX): validação neutra e contrato dos backends.

Cada backend de plataforma (mpm.platforms.<so>.accounts) expõe:

    is_elevated()                    -> bool | None
    lookup_user(name)                -> SID (str) | None     usuário LOCAL com esse nome
    create_user(name, password, admin) -> SID                cria a conta local (e desfaz se falhar)
    ensure_profile(sid, name)        -> Path                 cria (ou localiza) o perfil
    predict_profile_path(name)       -> Path                 onde o perfil deve ficar (sem criar nada)
    set_owner(path, name)            -> str | None           None = ok; texto = aviso
    check(name)                      -> list[(rótulo, valor, ok)]   diagnóstico somente leitura
"""

from __future__ import annotations


class AccountError(Exception):
    """Falha ao preparar a conta/perfil de destino."""


class NotSupported(AccountError):
    """Esta plataforma não sabe criar contas."""


_FORBIDDEN = set('"/\\[]:;|=,+*?<>@')
MAX_NAME = 20          # limite do nome de conta SAM no Windows


def validate_username(name: str) -> str:
    """Valida segundo as regras de conta local do Windows. Retorna o nome limpo."""
    name = (name or "").strip()
    if not name:
        raise AccountError("nome de usuário vazio")
    if len(name) > MAX_NAME:
        raise AccountError(f"nome de usuário com mais de {MAX_NAME} caracteres: {name!r}")
    bad = sorted({c for c in name if c in _FORBIDDEN or ord(c) < 32})
    if bad:
        raise AccountError(f"nome de usuário com caracteres não permitidos: {''.join(bad)!r}")
    if name.endswith(".") or name.isdigit():
        raise AccountError("nome de usuário não pode terminar em ponto nem ser só números")
    if name.upper() in {"CON", "PRN", "AUX", "NUL", "ADMINISTRATOR", "GUEST", "SYSTEM"}:
        raise AccountError(f"nome reservado: {name!r}")
    return name
