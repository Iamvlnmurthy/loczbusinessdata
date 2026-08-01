# Ingestion monitor UI. Restarts itself if the DB is briefly unavailable.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'
$log = Join-Path $root 'var\logs'
New-Item -ItemType Directory -Force -Path $log | Out-Null

while ($true) {
    $stamp = Get-Date -Format 'yyyy-MM-dd'
    & $py (Join-Path $root 'scripts\monitor.py') *>> (Join-Path $log "monitor-$stamp.log")
    Start-Sleep -Seconds 15   # exited: wait, then come back up
}
