"""Perfis de usuário existentes na máquina e seleção do perfil a migrar."""

from __future__ import annotations

from mpm import platforms
from mpm.core.models import ProfileInfo


class ProfileNotFound(ValueError):
    """Perfil pedido não existe (ou não há perfil atual identificável)."""


def list_profiles() -> list[ProfileInfo]:
    return platforms.current().list_profiles()


def select_profile(
    selector: str | None = None,
    profiles: list[ProfileInfo] | None = None,
) -> ProfileInfo:
    """Escolhe o perfil por nome de usuário ou SID; sem seletor, o usuário atual."""
    available = profiles if profiles is not None else list_profiles()
    names = ", ".join(p.username for p in available) or "(nenhum)"

    if selector is None:
        for profile in available:
            if profile.is_current:
                return profile
        raise ProfileNotFound(
            f"Não foi possível identificar o perfil atual; use --user. Disponíveis: {names}"
        )

    wanted = selector.strip().lower()
    for profile in available:
        if profile.username.lower() == wanted or profile.sid.lower() == wanted:
            return profile
    raise ProfileNotFound(f"Perfil {selector!r} não encontrado. Disponíveis: {names}")
