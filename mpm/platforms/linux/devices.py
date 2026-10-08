"""Wi-Fi do Windows: não suportado no Linux."""

from __future__ import annotations

from typing import Any


def _no(*_a: Any, **_k: Any):
    raise RuntimeError("Wi-Fi do Windows só é suportado no Windows")


def is_elevated() -> bool:
    return False


export_wifi_xml = existing_wifi_names = add_wifi_profile = _no
