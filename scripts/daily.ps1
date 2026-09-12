# Daily CRM refresh for Windows. Equivalent of daily.sh.
# Order matters - snapshot reads the views, which read the extracted records.
#
# Schedule it (run once, in an elevated PowerShell):
#   $action  = New-ScheduledTaskAction -Execute "powershell.exe" `
#              -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\path\to\crm\scripts\daily.ps1"
#   $trigger = New-ScheduledTaskTrigger -Daily -At 2am
#   $set     = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun
#   Register-ScheduledTask -TaskName "CRM daily refresh" -Action $action `
#              -Trigger $trigger -Settings $set
#
# -StartWhenAvailable is the important flag: it runs a missed job once the
# machine is back, instead of silently skipping that day's snapshot.

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

New-Item -ItemType Directory -Force -Path "logs" | Out-Null
$log = "logs\daily.log"

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -Append $log

try {
    python scripts\extract.py    *>&1 | Out-File -Append $log
    python scripts\transform.py  *>&1 | Out-File -Append $log
    python scripts\snapshot.py   *>&1 | Out-File -Append $log
    "=== done ===`n" | Out-File -Append $log
}
catch {
    "FAILED: $_`n" | Out-File -Append $log
    exit 1
}
