# HermesSync scheduled-task installer
#
# Registers a task that runs hermes-sync.py every 5 minutes.
# Run from any PowerShell:
#   powershell -ExecutionPolicy Bypass -File hermes-sync\setup-task.ps1
#
# Notes:
# - Uses Register-ScheduledTask so Settings.Enabled=true is written correctly.
# - schtasks /Create writes Enabled=false into the XML and /Change /ENABLE cannot fix it.
# - Paths use $env:USERPROFILE so the same script works on any account.
# - The Task Scheduler Enabled flag can be blocked by third-party security software;
#   whitelist the script and python.exe if the task refuses to enable.

$ErrorActionPreference = "Stop"

$TaskName = "HermesSync"
$Home     = $env:USERPROFILE
$Python   = Join-Path $Home "AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
$Script   = Join-Path $Home "hermes-sync\hermes-sync.py"

# Remove any existing task first
$old = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($old) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

# Trigger: every 5 minutes, repeating for ~10 years
# New-ScheduledTaskTrigger requires TimeSpan (not an ISO8601 string).
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval ([TimeSpan]::FromMinutes(5)) `
    -RepetitionDuration ([TimeSpan]::FromDays(3650))

# Settings: run on battery, allow missed runs, no duplicate instances
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`""

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Limited | Out-Null

# Enable (critical step -- ensures Settings.Enabled=true)
Enable-ScheduledTask -TaskName $TaskName | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
Write-Host ""
Write-Host "================ Task status ================"
Write-Host ("  Name           : " + $t.TaskName)
Write-Host ("  Enabled        : " + $t.Enabled)
Write-Host ("  State          : " + $t.State)
Write-Host ("  Interval       : " + $t.Triggers[0].Repetition.Interval)
Write-Host ("  Run on battery : " + (-not $t.Settings.DisallowStartIfOnBatteries))
Write-Host ("  Run missed     : " + $t.Settings.StartWhenAvailable)
Write-Host ("  Command        : " + $t.Actions[0].Execute)
Write-Host ("  Args           : " + $t.Actions[0].Arguments)
Write-Host "==========================================="
Write-Host ""
Write-Host ("Manual test : schtasks /Run /TN ""$TaskName"""
Write-Host ""
Write-Host "If Enabled stays false, a security tool may be blocking it."
Write-Host "Whitelist the script + python.exe, then re-run this installer."
