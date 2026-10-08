"""Ponto de entrada do mpm.exe (PyInstaller). Sem argumentos, abre o menu simples."""

import sys

from mpm.cli import main


def _run() -> int:
    if len(sys.argv) == 1 and getattr(sys, "frozen", False):
        from mpm import platforms
        from mpm.launcher import run_launcher, screen_clear
        try:
            elevated = platforms.accounts().is_elevated()
        except Exception:                      # noqa: BLE001 - o aviso é só informativo
            elevated = None
        return run_launcher(main, elevated=elevated, clear=screen_clear)
    return main()


if __name__ == "__main__":
    sys.exit(_run())
