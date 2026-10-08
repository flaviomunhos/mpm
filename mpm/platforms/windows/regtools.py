"""Exportar/importar chaves do registro com `reg.exe` e carregar o hive de outro usuário."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from mpm.core.appdata import retarget_reg

MAX_EXPORT_BYTES = 4 * 1024 * 1024


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _message(run: subprocess.CompletedProcess) -> str:
    return (run.stderr or run.stdout or f"reg retornou {run.returncode}").strip().splitlines()[-1][:200]


def export_key(key: str, hive: str | None = None) -> bytes | None:
    """Conteúdo (.reg, UTF-16) de HKCU\\<key> (ou HKU\\<hive>\\<key>); None se a chave não existir.

    Com `hive` os cabeçalhos voltam a `HKEY_CURRENT_USER`, para o RX tratar igual nos dois casos.
    """
    source = f"HKU\\{hive}\\{key}" if hive else f"HKCU\\{key}"
    with tempfile.TemporaryDirectory(prefix="mpm-reg-") as tmp:
        out = Path(tmp) / "export.reg"
        run = _run(["reg", "export", source, str(out), "/y"])
        if run.returncode != 0 or not out.is_file():
            return None
        data = out.read_bytes()
    if len(data) > MAX_EXPORT_BYTES:
        raise ValueError(f"exportação de {key!r} grande demais ({len(data)} bytes)")
    if hive:
        text = data.decode("utf-16")
        text = re.sub(r"(?im)^(\[-?)HKEY_USERS\\" + re.escape(hive) + r"\\", r"\1HKEY_CURRENT_USER\\", text)
        data = text.encode("utf-16")
    return data


@contextmanager
def user_hive(sid: str, ntuser: Path) -> Iterator[str]:
    """Nome do hive de um usuário em HKEY_USERS: o SID, se ele tem sessão aberta; senão carrega o NTUSER.DAT."""
    if _run(["reg", "query", f"HKU\\{sid}"]).returncode == 0:
        yield sid
        return
    name = f"MPM_TX_{secrets.token_hex(4).upper()}"
    error = load_hive(name, ntuser)
    if error:
        raise RuntimeError(f"não consegui abrir o registro do usuário ({error}); rode o TX como administrador")
    try:
        yield name
    finally:
        unload_hive(name)


def load_hive(name: str, ntuser: Path) -> str | None:
    """Carrega o NTUSER.DAT em HKEY_USERS\\<name>. Retorna o erro (texto) ou None."""
    if not ntuser.is_file():
        return f"não encontrei {ntuser}"
    run = _run(["reg", "load", f"HKU\\{name}", str(ntuser)])
    return None if run.returncode == 0 else _message(run)


def unload_hive(name: str) -> str | None:
    run = _run(["reg", "unload", f"HKU\\{name}"])
    return None if run.returncode == 0 else _message(run)


def import_reg(data: bytes, hive_name: str | None = None) -> str | None:
    """Importa um .reg. Com `hive_name`, aponta os cabeçalhos para HKEY_USERS\\<hive_name>."""
    text = data.decode("utf-16")
    if hive_name:
        text = retarget_reg(text, hive_name)
    fd, tmp = tempfile.mkstemp(prefix="mpm-reg-", suffix=".reg")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-16"))
        run = _run(["reg", "import", tmp])
        return None if run.returncode == 0 else _message(run)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
