# Maintenance worker. Claims jobs from the Postgres queue and runs the periodic
# housekeeping the engine needs whether or not anyone is logged in.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$py  = Join-Path $root '.venv\Scripts\python.exe'
$log = Join-Path $root 'var\logs'
New-Item -ItemType Directory -Force -Path $log | Out-Null

while ($true) {
    $stamp = Get-Date -Format 'yyyy-MM-dd'
    & $py (Join-Path $root 'scripts\worker.py') *>> (Join-Path $log "worker-$stamp.log")
    Start-Sleep -Seconds 30
}
