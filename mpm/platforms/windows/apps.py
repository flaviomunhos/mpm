"""Programas instalados no Windows: registro (Uninstall) e `winget export`. Somente leitura."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


def _value(key, name: str) -> Any:
    import winreg
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def list_installed_apps() -> list[dict[str, Any]]:
    """Lê HKLM (64 e 32 bits) e HKCU do usuário que executa o TX."""
    import winreg
    places = [
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY, "machine64"),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY, "machine32"),
        (winreg.HKEY_CURRENT_USER, 0, "user"),
    ]
    apps: list[dict[str, Any]] = []
    for hive, flag, scope in places:
        try:
            root = winreg.OpenKey(hive, _UNINSTALL, 0, winreg.KEY_READ | flag)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    key = winreg.OpenKey(root, sub)
                except OSError:
                    continue
                with key:
                    name = _value(key, "DisplayName")
                    release = str(_value(key, "ReleaseType") or "")
                    apps.append({
                        "name": str(name).strip() if name else "",
                        "version": str(_value(key, "DisplayVersion") or ""),
                        "publisher": str(_value(key, "Publisher") or ""),
                        "install_date": str(_value(key, "InstallDate") or ""),
                        "install_location": str(_value(key, "InstallLocation") or ""),
                        "key": sub,
                        "scope": scope,
                        "system_component": bool(_value(key, "SystemComponent")),
                        "is_update": bool(_value(key, "ParentKeyName"))
                                     or release in ("Update", "Hotfix", "Security Update", "Update Rollup"),
                        "msi": bool(_value(key, "WindowsInstaller")),
                    })
    return apps


# Códigos do winget (HRESULT) que significam "nada a fazer".
_ALREADY = {0x8A150061: "já instalado", 0x8A15002B: "já instalado (sem atualização aplicável)"}


def winget_exists(pkg_id: str, timeout: int = 120) -> bool:
    """O ID existe no repositório do winget? (`winget show --exact`; não instala nada)"""
    exe = shutil.which("winget")
    if not exe:
        raise RuntimeError("winget não encontrado nesta máquina")
    run = subprocess.run(
        [exe, "show", "--id", pkg_id, "--exact", "--source", "winget",
         "--accept-source-agreements", "--disable-interactivity"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return run.returncode == 0


def winget_install(pkg_id: str, version: str | None = None, timeout: int = 1800) -> tuple[str, str]:
    """Instala um pacote em modo silencioso. Retorna (status, detalhe): instalado | ja_instalado | falhou."""
    exe = shutil.which("winget")
    if not exe:
        raise RuntimeError("winget não encontrado nesta máquina")
    cmd = [exe, "install", "--id", pkg_id, "--exact", "--source", "winget", "--silent",
           "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
    if version:
        cmd += ["--version", version]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=timeout)
    except subprocess.TimeoutExpired:
        return "falhou", f"tempo esgotado ({timeout}s)"
    code = run.returncode & 0xFFFFFFFF
    if code == 0:
        return "instalado", ""
    if code in _ALREADY:
        return "ja_instalado", _ALREADY[code]
    tail = (run.stdout or run.stderr or "").strip().splitlines()[-1:] or ["sem saída"]
    return "falhou", f"código 0x{code:08X}: {tail[0][:160]}"


def winget_export(timeout: int = 300) -> dict[str, Any]:
    """JSON do `winget export` (só pacotes que o winget reconhece, com ID e versão)."""
    exe = shutil.which("winget")
    if not exe:
        raise RuntimeError("winget não encontrado nesta máquina")
    with tempfile.TemporaryDirectory(prefix="mpm-apps-") as tmp:
        out = Path(tmp) / "export.json"
        run = subprocess.run(
            [exe, "export", "-o", str(out), "--include-versions",
             "--accept-source-agreements", "--disable-interactivity"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if not out.is_file():      # código != 0 ainda pode gerar o arquivo (pacotes indisponíveis)
            tail = (run.stdout or run.stderr or "").strip().splitlines()[-1:] or ["sem saída"]
            raise RuntimeError(f"winget export falhou (código {run.returncode}): {tail[0][:200]}")
        return json.loads(out.read_text(encoding="utf-8-sig"))
