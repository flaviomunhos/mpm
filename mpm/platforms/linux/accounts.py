"""Contas no Linux: não suportado (o destino do MPM é Windows). Existe só para dar erro claro."""

from __future__ import annotations

from pathlib import Path

from mpm.core.accounts import NotSupported


def _no(*_a, **_k):
    raise NotSupported("criar usuário/perfil só é suportado no Windows")


def is_elevated() -> bool | None:
    return None


lookup_user = create_user = ensure_profile = predict_profile_path = existing_profile_path = set_owner = _no


def check(name: str) -> list[tuple[str, str, bool]]:
    return [("plataforma", "Linux: criação de contas não suportada", False)]
