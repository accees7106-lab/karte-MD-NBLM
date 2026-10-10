# Show what the karte OCR worker is doing on this PC, and optionally restart it.
# ASCII only on purpose (PowerShell 5.1).
#   powershell -ExecutionPolicy Bypass -File .\status.ps1            # show status
#   powershell -ExecutionPolicy Bypass -File .\status.ps1 -Restart   # restart the worker safely
# -Restart stops ONLY the worker (python worker.py) and its child processes (claude -p).
# It never touches the Claude desktop app or other Claude Code sessions.
param([switch]$Restart)

$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = 'KarteOcrWorker'

function Get-WorkerProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*worker.py*' }
}

function Show-Status {
    Write-Host '== Task' -ForegroundColor Cyan
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
        Write-Host ('State: ' + $task.State + '   Last run: ' + $info.LastRunTime + '   Last result: ' + $info.LastTaskResult)
    } else {
        Write-Host 'Task KarteOcrWorker is not registered (run setup.ps1).' -ForegroundColor Yellow
    }

    Write-Host ''
    Write-Host '== Worker processes' -ForegroundColor Cyan
    $workers = @(Get-WorkerProcesses)
    if ($workers.Count -eq 0) { Write-Host 'No worker process is running.' -ForegroundColor Yellow }
    foreach ($w in $workers) {
        Write-Host ('python worker.py  PID ' + $w.ProcessId + '  started ' + $w.CreationDate)
        $kids = @(Get-CimInstance Win32_Process -Filter ("ParentProcessId=" + $w.ProcessId) -ErrorAction SilentlyContinue)
        foreach ($k in $kids) {
            $mins = [int]((Get-Date) - $k.CreationDate).TotalMinutes
            $line = ('  child ' + $k.Name + '  PID ' + $k.ProcessId + '  running ' + $mins + ' min')
            if ($mins -ge 6) {
                Write-Host ($line + '   <-- probably stuck. Run: .\status.ps1 -Restart') -ForegroundColor Yellow
            } else {
                Write-Host $line
            }
        }
    }

    Write-Host ''
    Write-Host '== Last log lines (ocr-worker.log)' -ForegroundColor Cyan
    $log = Join-Path $here 'ocr-worker.log'
    if (Test-Path $log) { Get-Content $log -Tail 15 -Encoding UTF8 } else { Write-Host '(no log yet)' }

    $err = Join-Path $here 'ocr-worker.err.log'
    if ((Test-Path $err) -and ((Get-Item $err).Length -gt 0)) {
        Write-Host ''
        Write-Host '== Last crash output (ocr-worker.err.log)' -ForegroundColor Cyan
        Get-Content $err -Tail 10
    }
}

if ($Restart) {
    Write-Host 'Restarting the worker...' -ForegroundColor Cyan
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    foreach ($w in @(Get-WorkerProcesses)) {
        & taskkill /PID $w.ProcessId /T /F | Out-Null
        Write-Host ('Stopped worker PID ' + $w.ProcessId + ' (and its child processes)')
    }
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 8
    Write-Host ''
}

Show-Status
