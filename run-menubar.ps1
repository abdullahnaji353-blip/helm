# Windows launcher for Helm's tray UI — the Windows counterpart of run-menubar.sh.
#
# Unlike the macOS build (which compiles a signed .app so TCC permissions
# stick across rebuilds), there is no code-signing step here: Windows has no
# equivalent permission system tied to a binary's signing identity.

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$VenvPath = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating virtual environment..."
    python -m venv $VenvPath
}

Write-Host "Installing dependencies..."
& $VenvPython -m pip install --upgrade pip | Out-Null
& $VenvPython -m pip install -r (Join-Path $ProjectRoot "requirements.txt")

Write-Host "Starting Helm..."
& $VenvPython -m windows_tray.app
