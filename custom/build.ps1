# Build MacroStudio.exe locally. Run from the custom/ directory:
#   powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
python -m pip install --upgrade pyinstaller
python -m pip install -r requirements.txt
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
python -m PyInstaller --clean --noconfirm macro_studio.spec
Write-Host "Built: $(Resolve-Path dist/MacroStudio.exe)"
