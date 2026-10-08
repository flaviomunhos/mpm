"""User Discovery — identifica quem está executando o MPM."""

from __future__ import annotations

from mpm import platforms
from mpm.core.models import UserInfo


def discover_user() -> UserInfo:
    return platforms.current().discover_user()
