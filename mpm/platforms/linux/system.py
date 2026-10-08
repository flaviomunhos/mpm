from __future__ import annotations

import platform
from pathlib import Path


def _read_os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    for candidate in ("/etc/os-release", "/usr/lib/os-release"):
        path = Path(candidate)
        if not path.is_file():
            continue
        for line in path.read_text(errors="replace").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip().strip('"')
        break
    return data


def discover_os() -> tuple[str, str, str | None]:
    """Retorna (os_name, os_release, os_build)."""
    info = _read_os_release()
    name = info.get("PRETTY_NAME") or platform.system()
    release = info.get("VERSION_ID") or platform.release()
    return name, release, platform.release()
