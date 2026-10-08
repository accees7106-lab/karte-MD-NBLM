# Register scheduled task "KarteOcrWorker" that starts run_worker.ps1 at logon.
# Run once in PowerShell as the normal user (no admin needed):
#   powershell -ExecutionPolicy Bypass -File .\register_task.ps1
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'run_worker.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ("-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"{0}`"" -f $script) `
    -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName 'KarteOcrWorker' -Action $action -Trigger $trigger `
    -Settings $settings -Description 'karte handwritten note OCR worker (claude -p)' -Force
Start-ScheduledTask -TaskName 'KarteOcrWorker'
Get-ScheduledTask -TaskName 'KarteOcrWorker' | Select-Object TaskName, State
