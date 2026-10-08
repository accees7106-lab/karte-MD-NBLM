# Keepalive loop for karte ocr-worker. Restarts worker.py 60s after it exits.
# Log: ocr-worker.log (same folder)
$ErrorActionPreference = 'Continue'
# Never bill the API: drop ANTHROPIC_API_KEY for this process only (other tools keep it).
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
$env:PYTHONIOENCODING = 'utf-8'
$log = Join-Path $here 'ocr-worker.log'
while ($true) {
    python worker.py *>> $log
    Add-Content -Path $log -Value ("[{0}] worker exited (code {1}), restart in 60s" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $LASTEXITCODE)
    Start-Sleep -Seconds 60
}
