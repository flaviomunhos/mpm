"""System Discovery — genérico; delega o que for específico à plataforma."""

from __future__ import annotations

import platform
from datetime import datetime, timezone

from mpm import platforms
from mpm.core.models import SystemInfo


def discover_system() -> SystemInfo:
    os_name, os_release, os_build = platforms.current().discover_os()
    return SystemInfo(
        hostname=platform.node(),
        os_family=platform.system(),
        os_name=os_name,
        os_release=os_release,
        os_build=os_build,
        architecture=platform.machine(),
        python_version=platform.python_version(),
        platform_string=platform.platform(),
        collected_at=datetime.now(timezone.utc).isoformat(),
    )
