from __future__ import annotations

import csv
import io
import os
import subprocess
from pathlib import Path

from mpm.core.models import UserInfo


def _whoami_sid() -> str | None:
    """SID do usuário atual via `whoami /user` (ferramenta nativa do Windows)."""
    try:
        out = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
        row = next(csv.reader(io.StringIO(out)))
        return row[1].strip()
    except (OSError, subprocess.SubprocessError, StopIteration, IndexError):
        return None


def _is_elevated() -> bool | None:
    """True só se o processo está elevado (UAC), não apenas se o usuário é admin."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None


def discover_user() -> UserInfo:
    username = os.environ.get("USERNAME") or os.getlogin()
    domain = os.environ.get("USERDOMAIN")
    computer = os.environ.get("COMPUTERNAME")
    profile = os.environ.get("USERPROFILE") or str(Path.home())

    # Máquina fora de domínio: USERDOMAIN == COMPUTERNAME (workgroup/local).
    joined = None
    if domain and computer:
        joined = domain.upper() != computer.upper()

    return UserInfo(
        username=username,
        user_id=_whoami_sid() or "",
        group_id=None,
        profile_path=profile,
        domain=domain,
        domain_joined=joined,
        shell=None,
        is_elevated=_is_elevated(),
        extra={"computer": computer, "logon_server": os.environ.get("LOGONSERVER")},
    )
