"""Perfis de Wi-Fi (netsh). Ler = TX; criar = RX (precisa de administrador)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def _last_line(run: subprocess.CompletedProcess) -> str:
    text = (run.stderr or run.stdout or f"código {run.returncode}").strip().splitlines()
    return text[-1][:240] if text else f"código {run.returncode}"


def is_elevated() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


# ------------------------------------------------------------------------------------------------ TX (leitura)
def export_wifi_xml() -> list[str]:
    """XML de todos os perfis de Wi-Fi. Com administrador as senhas vêm em claro (`key=clear`)."""
    tmp = tempfile.mkdtemp(prefix="mpm-wlan-")
    try:
        run = subprocess.run(["netsh", "wlan", "export", "profile", "key=clear", f"folder={tmp}"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        files = sorted(Path(tmp).glob("*.xml"))
        if not files and run.returncode != 0:
            raise RuntimeError(_last_line(run))
        return [f.read_bytes().decode("utf-8-sig") for f in files]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------------------------------------ RX (escrita)
def existing_wifi_names() -> set[str]:
    """Nomes dos perfis de Wi-Fi já salvos neste computador (exporta sem senha só para ler os nomes)."""
    tmp = tempfile.mkdtemp(prefix="mpm-wlan-")
    try:
        subprocess.run(["netsh", "wlan", "export", "profile", f"folder={tmp}"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
        from mpm.core.devices import parse_wifi_xml
        names = set()
        for f in Path(tmp).glob("*.xml"):
            try:
                names.add(parse_wifi_xml(f.read_bytes().decode("utf-8-sig"))["name"].casefold())
            except ValueError:
                continue
        return names
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def add_wifi_profile(xml: str) -> str | None:
    """Importa um perfil de Wi-Fi para todos os usuários. Retorna o erro (texto) ou None."""
    fd, tmp = tempfile.mkstemp(prefix="mpm-wlan-", suffix=".xml")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(xml.encode("utf-8"))
        run = subprocess.run(["netsh", "wlan", "add", "profile", f"filename={tmp}", "user=all"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        return None if run.returncode == 0 else _last_line(run)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
