from __future__ import annotations

import platform


def _registry_value(name: str) -> str | None:
    try:
        import winreg  # só existe no Windows
    except ImportError:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        ) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except OSError:
        return None


def discover_os() -> tuple[str, str, str | None]:
    """Retorna (os_name, os_release, os_build).

    Obs.: o ProductName do registro ainda diz "Windows 10" em algumas
    builds do Windows 11; build >= 22000 indica Windows 11.
    """
    product = _registry_value("ProductName") or f"Windows {platform.release()}"
    build = _registry_value("CurrentBuildNumber") or platform.version().split(".")[-1]
    display = _registry_value("DisplayVersion") or _registry_value("ReleaseId") or platform.release()

    if build and build.isdigit() and int(build) >= 22000:
        product = product.replace("Windows 10", "Windows 11")

    return product, display, build
