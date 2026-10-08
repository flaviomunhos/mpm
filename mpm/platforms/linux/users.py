from __future__ import annotations

import os
import pwd
import socket

from mpm.core.models import UserInfo


def discover_user() -> UserInfo:
    uid = os.getuid()
    entry = pwd.getpwuid(uid)
    return UserInfo(
        username=entry.pw_name,
        user_id=str(uid),
        group_id=str(entry.pw_gid),
        profile_path=entry.pw_dir,
        domain=socket.gethostname(),
        domain_joined=None,
        shell=entry.pw_shell,
        is_elevated=(uid == 0),
        extra={"gecos": entry.pw_gecos, "euid": os.geteuid()},
    )
