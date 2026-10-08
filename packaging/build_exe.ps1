# Gera dist\mpm-<versão>-win64.zip com o mpm.exe (pasta portátil). Rode no Windows, dentro de packaging\ ou na raiz.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

python -m venv .build-venv
.\.build-venv\Scripts\python.exe -m pip install --upgrade pip
.\.build-venv\Scripts\python.exe -m pip install pyinstaller "cryptography>=42"

$version = (.\.build-venv\Scripts\python.exe -c "import mpm; print(mpm.__version__)").Trim()
.\.build-venv\Scripts\python.exe -m PyInstaller packaging\mpm.spec --noconfirm --clean --distpath dist --workpath build

$zip = "dist\mpm-$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path dist\mpm\* -DestinationPath $zip
Write-Host "Pronto: $zip"
Write-Host "Teste: dist\mpm\mpm.exe --version   e   dist\mpm\mpm.exe discover"
Get-FileHash $zip -Algorithm SHA256 | Format-List
