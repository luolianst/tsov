# tsov web launcher - starts the visual host shell in this PowerShell window
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent   # project root
Set-Location $root
$host.UI.RawUI.WindowTitle = "tsov web - http://127.0.0.1:8790"

Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "  tsov - visual host shell"                    -ForegroundColor Cyan
Write-Host "  URL      : http://127.0.0.1:8790"           -ForegroundColor Cyan
Write-Host "  browser  : auto-open in ~4s"                -ForegroundColor Cyan
Write-Host "  stop     : Ctrl+C or close this window"     -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

$exe = Join-Path $root "tsov\.venv\Scripts\tsov.exe"
if (-not (Test-Path $exe)) {
    Write-Host "[ERROR] tsov.exe not found: $exe" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

# auto-open browser shortly after the server binds
Start-Job -ScriptBlock { Start-Sleep -Seconds 4; Start-Process "http://127.0.0.1:8790" } | Out-Null

try {
    & $exe web --host 127.0.0.1 --port 8790
} catch {
    Write-Host ""
    Write-Host ("[ERROR] " + $_.Exception.Message) -ForegroundColor Red
}
Write-Host ""
Write-Host "[tsov] server exited." -ForegroundColor Yellow