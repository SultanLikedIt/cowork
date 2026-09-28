# Starts the local computer / Outlook bridge with the token from ../.env.
#
# Right-click -> "Run with PowerShell", or from the proje folder:
#     powershell -ExecutionPolicy Bypass -File scripts\start_bridge.ps1
#
# Without the token the bridge still starts, but its Outlook endpoints stay
# off ("token_configured": false) -- the usual reason mail "does nothing".
# Keep the window open: closing it stops the bridge.

$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root ".env"
$line = Get-Content $envFile -ErrorAction Stop | Select-String '^\s*LOCAL_COMPUTER_TOKEN\s*='
if (-not $line) {
    Write-Host "LOCAL_COMPUTER_TOKEN not found in $envFile" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}
$env:LOCAL_COMPUTER_TOKEN = ($line.Line -split '=', 2)[1].Trim()

$running = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "Port 8765 is already in use (process $($running.OwningProcess)). Stop that bridge first." -ForegroundColor Yellow
    Read-Host "Press Enter to close"
    exit 1
}

Set-Location $root
python scripts\local_computer_bridge.py
