"""Registro do Windows: não existe no Linux."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def export_key(key: str, hive: str | None = None) -> bytes | None:
    raise RuntimeError("o registro só existe no Windows")


def load_hive(name: str, ntuser: Path) -> str | None:
    raise RuntimeError("o registro só existe no Windows")


def unload_hive(name: str) -> str | None:
    raise RuntimeError("o registro só existe no Windows")


def import_reg(data: bytes, hive_name: str | None = None) -> str | None:
    raise RuntimeError("o registro só existe no Windows")


@contextmanager
def user_hive(sid: str, ntuser: Path) -> Iterator[str]:
    raise RuntimeError("o registro só existe no Windows")
    yield ""
