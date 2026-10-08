"""Dados de aplicativos no Linux: não suportado (o foco do MPM é Windows)."""

from __future__ import annotations

from pathlib import Path


def roots() -> dict[str, Path]:
    raise RuntimeError("dados de aplicativos só são suportados no Windows")


def roots_for(profile: Path) -> dict[str, Path]:
    raise RuntimeError("dados de aplicativos só são suportados no Windows")


def registry_key_exists(key: str, hive: str | None = None) -> bool:
    return False
