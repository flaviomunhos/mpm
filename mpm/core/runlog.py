"""Log da execução: tudo o que o programa mostra na tela também vai para um arquivo com data e hora.

- Pasta: %ProgramData%\\MPM\\logs (Windows) ou ~/.mpm/logs; `MPM_LOG_DIR` troca a pasta.
- Um arquivo por execução: mpm-<comando>-AAAAMMDD-HHMMSS.log. Guarda os 30 mais recentes.
- Segredos não entram: código de pareamento, `--code`, senhas e chaves Wi-Fi são trocados por ***.
  O que é digitado (getpass/input) não passa pela saída, então também não é gravado.
- Nunca atrapalha a execução: qualquer erro ao gravar o log é ignorado.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

KEEP = 30

_REDACT = [
    (re.compile(r"(?i)(c[óo]digo de pareamento\s*:\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(--code[ =]+)\S+"), r"\1***"),
    (re.compile(r"(?i)\b(senha|password|passphrase|keyMaterial)\b(\s*[:=]\s*)\S+"), r"\1\2***"),
    (re.compile(r"(?i)(<keyMaterial>)[^<]*(</keyMaterial>)"), r"\1***\2"),
]


_PROGRESS = re.compile(r"^\s*\d+/\d+ arq\.")


def redact(text: str) -> str:
    for pattern, repl in _REDACT:
        text = pattern.sub(repl, text)
    return text


def log_dir() -> Path:
    override = os.environ.get("MPM_LOG_DIR")
    if override:
        return Path(override)
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        return Path(program_data) / "MPM" / "logs"
    return Path.home() / ".mpm" / "logs"


class _Tee:
    """Repassa tudo ao fluxo original e copia as linhas completas para o log."""

    def __init__(self, stream: Any, tag: str, owner: RunLog):
        self._stream = stream
        self._tag = tag
        self._owner = owner
        self._buf = ""

    def write(self, text: str) -> int:
        written = self._stream.write(text)
        try:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._owner.line(self._tag, line)
        except Exception:                      # noqa: BLE001 - o log nunca derruba a execução
            pass
        return written if isinstance(written, int) else len(text)

    def flush(self) -> None:
        self._stream.flush()

    def flush_pending(self) -> None:
        if self._buf:
            self._owner.line(self._tag, self._buf)
            self._buf = ""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class RunLog:
    def __init__(self) -> None:
        self.path: Path | None = None
        self._file: Any = None
        self._lock = threading.Lock()
        self._tees: list[tuple[str, _Tee, Any]] = []

    @classmethod
    def start(cls, command: str, argv: list[str]) -> RunLog:
        run = cls()
        try:
            folder = log_dir()
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            run.path = folder / f"mpm-{command}-{stamp}-{os.getpid()}.log"
            run._file = run.path.open("a", encoding="utf-8", newline="\n")
            run._prune(folder)
        except OSError:
            run._file = None
            run.path = None
            return run
        from mpm import __version__
        run.line("INFO", f"MPM {__version__} · comando: {command} · argumentos: {' '.join(argv)}")
        run.line("INFO", f"Python {sys.version.split()[0]} · {sys.platform} · pasta: {os.getcwd()}")
        for tag, name in (("OUT", "stdout"), ("ERR", "stderr")):
            original = getattr(sys, name)
            tee = _Tee(original, tag, run)
            setattr(sys, name, tee)
            run._tees.append((name, tee, original))
        return run

    def _prune(self, folder: Path) -> None:
        try:
            old = sorted(folder.glob("mpm-*.log"), key=lambda p: p.stat().st_mtime)[:-KEEP]
            for path in old:
                path.unlink()
        except OSError:
            pass

    def line(self, tag: str, text: str) -> None:
        if self._file is None:
            return
        text = redact(text.split("\r")[-1].rstrip())      # barra de progresso (\r): fica o último estado
        if not text:
            return
        if tag == "ERR" and _PROGRESS.match(text):
            tag = "PROG"                                   # a barra de progresso sai pelo stderr, mas não é erro
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._lock:
                self._file.write(f"{stamp} [{tag}] {text}\n")
                self._file.flush()
        except (OSError, ValueError):
            pass

    def exception(self) -> None:
        for row in traceback.format_exc().splitlines():
            self.line("ERR", row)

    def stop(self, code: object) -> None:
        for _name, tee, _orig in self._tees:
            tee.flush_pending()
        self.line("INFO", f"Fim · código de saída: {code}")
        for name, _tee, original in self._tees:
            setattr(sys, name, original)
        self._tees.clear()
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
