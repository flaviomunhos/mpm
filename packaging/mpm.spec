# PyInstaller: `pyinstaller packaging/mpm.spec` (rode no Windows; use packaging/build_exe.ps1)
# onedir (uma pasta com mpm.exe + bibliotecas), console. Sem manifesto de administrador: abra o PowerShell
# (ou o mpm.exe) "como administrador", igual ao uso com `python -m mpm`.
import os
from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))      # raiz do projeto (onde fica a pasta mpm/)

hidden = collect_submodules("mpm")            # inclui mpm.platforms.windows.* (importados dentro de funções)

a = Analysis(
    [os.path.join(SPECPATH, "mpm_entry.py")],
    pathex=[ROOT],
    hiddenimports=hidden,
    excludes=["tkinter", "unittest.mock", "pydoc", "test", "tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [("u", None, "OPTION")],      # stdout/stderr sem buffer (progresso aparece ao redirecionar)
    exclude_binaries=True,
    name="mpm",
    console=True,
    upx=False,                                  # UPX aumenta falsos positivos do antivírus
)
coll = COLLECT(exe, a.binaries, a.datas, name="mpm", upx=False)
