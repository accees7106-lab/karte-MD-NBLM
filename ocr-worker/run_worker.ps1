# Keepalive loop for karte ocr-worker. Restarts worker.py 60s after it exits.
# Log: ocr-worker.log (same folder)
$ErrorActionPreference = 'Continue'
# Never bill the API: drop ANTHROPIC_API_KEY for this process only (other tools keep it).
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
$env:PYTHONIOENCODING = 'utf-8'
# The worker writes its own UTF-8 log (Japanese stays readable). Only crash output goes to the .err file.
$env:KARTE_LOG = Join-Path $here 'ocr-worker.log'
$err = Join-Path $here 'ocr-worker.err.log'
while ($true) {
    python worker.py 1>$null 2>>$err
    Add-Content -Path $env:KARTE_LOG -Value ("[{0}] worker exited (code {1}), restart in 60s" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $LASTEXITCODE) -Encoding ASCII
    Start-Sleep -Seconds 60
}
