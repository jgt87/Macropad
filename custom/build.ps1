# Build MacroStudio.exe locally. Run from the custom/ directory:
#   powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
python -m pip install --upgrade pyinstaller
python -m pip install -r requirements.txt

# The frozen app keeps its state next to the .exe (dist\macros.json / dist\layout.json).
# Those are the ONLY record of the user's macros - the device firmware is write-only and
# can't be read back - so preserve them across the dist wipe instead of destroying them.
$state = "macros.json", "layout.json"
$saved = @{}
foreach ($f in $state) {
    $p = Join-Path "dist" $f
    if (Test-Path $p) { $saved[$f] = [System.IO.File]::ReadAllText((Resolve-Path $p)) }
}

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
python -m PyInstaller --clean --noconfirm macro_studio.spec

# Write with a BOM-free UTF-8 encoder: the app parses macros.json as plain utf-8 and a BOM
# (which Set-Content -Encoding UTF8 emits on PS 5.1) would make json.load fail -> lost config.
$utf8 = New-Object System.Text.UTF8Encoding($false)
foreach ($f in $saved.Keys) {
    $p = Join-Path "dist" $f
    if (-not (Test-Path $p)) {
        [System.IO.File]::WriteAllText((Join-Path (Resolve-Path "dist") $f), $saved[$f], $utf8)
        Write-Host "Preserved existing $f"
    }
}
Write-Host "Built: $(Resolve-Path dist/MacroStudio.exe)"
