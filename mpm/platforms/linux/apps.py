"""Inventário de aplicativos no Linux: não suportado (o foco do MPM é Windows)."""

from __future__ import annotations

from typing import Any


def list_installed_apps() -> list[dict[str, Any]]:
    raise RuntimeError("inventário de aplicativos só é suportado no Windows")


def winget_exists(pkg_id: str, timeout: int = 120) -> bool:
    raise RuntimeError("winget só existe no Windows")


def winget_install(pkg_id: str, version: str | None = None, timeout: int = 1800) -> tuple[str, str]:
    raise RuntimeError("winget só existe no Windows")


def winget_export(timeout: int = 300) -> dict[str, Any]:
    raise RuntimeError("winget só existe no Windows")
