# One-time setup of the karte OCR worker on a Windows laptop.
# Works with Windows PowerShell 5.1 and PowerShell 7. No admin rights needed.
# ASCII only on purpose: PowerShell 5.1 can fail to parse UTF-8 Japanese in .ps1 files.
#
# Run from this folder (once; running it again is safe):
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# What it does. It never touches the karte web app; it only prepares THIS PC to run the worker.
#   1. checks Python 3.10+ (offers to install it with winget)
#   2. checks that Claude Code is installed
#   3. checks that ANTHROPIC_API_KEY is not going to be used (the worker never bills the API)
#   4. creates .env and stores GITHUB_TOKEN (input is hidden; the file is readable only by you)
#   5. runs "python worker.py --check": token, Vault access, and a real "claude -p" call
#   6. runs "python worker.py --once" (processes photos that are already waiting, if any)
#   7. offers to register the logon task "KarteOcrWorker" (keeps the worker running)
#
# Options: -SkipSmokeTest (skip step 6)   -SkipTask (skip step 7)
param(
    [switch]$SkipSmokeTest,
    [switch]$SkipTask
)

$ErrorActionPreference = 'Continue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

# The worker prints Japanese; make the console and Python agree on UTF-8.
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Show-Step($text) { Write-Host ''; Write-Host ('== ' + $text) -ForegroundColor Cyan }
function Show-Ok($text)   { Write-Host ('[OK] ' + $text) -ForegroundColor Green }
function Show-Note($text) { Write-Host ('[..] ' + $text) -ForegroundColor Yellow }
function Show-Ng($text)   { Write-Host ('[NG] ' + $text) -ForegroundColor Red }

function Ask-YesNo($question, $defaultYes) {
    if ($defaultYes) { $hint = '[Y/n]' } else { $hint = '[y/N]' }
    $answer = Read-Host ($question + ' ' + $hint)
    if ([string]::IsNullOrWhiteSpace($answer)) { return $defaultYes }
    return ($answer.Trim().ToLower().StartsWith('y'))
}

function Get-PythonVersion {
    # The Microsoft Store "python" stub prints a hint instead of a version; treat that as "not installed".
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { return $null }
    $out = & python --version 2>&1 | Out-String
    if ($out -match 'Python (\d+)\.(\d+)\.(\d+)') {
        return New-Object System.Version -ArgumentList ([int]$Matches[1]), ([int]$Matches[2]), ([int]$Matches[3])
    }
    return $null
}

Write-Host 'karte OCR worker setup' -ForegroundColor White
Write-Host ('Folder: ' + $here)

# ---------------------------------------------------------------- 1. Python
Show-Step '1/7 Python'
$py = Get-PythonVersion
if (($null -eq $py) -or ($py -lt (New-Object System.Version -ArgumentList 3, 10))) {
    if ($null -eq $py) { Show-Ng 'Python was not found.' } else { Show-Ng ('Python ' + $py + ' is too old (need 3.10 or newer).') }
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        if (Ask-YesNo 'Install Python 3.12 now with winget?' $false) {
            & winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
            Write-Host ''
            Show-Note 'Python was installed. Close this window, open a NEW PowerShell, and run this script again.'
            exit 0
        }
    }
    Show-Note 'Install Python 3.10+ from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this script again.'
    exit 1
}
Show-Ok ('Python ' + $py)

# ---------------------------------------------------------------- 2. Claude Code
Show-Step '2/7 Claude Code'
$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    Show-Ng 'Claude Code (the "claude" command) was not found.'
    Show-Note 'Install it from https://docs.claude.com/en/docs/claude-code/setup (it is not installed automatically).'
    Show-Note 'Then open a NEW PowerShell, run "claude", type /login and sign in with your Claude subscription account. Then run this script again.'
    exit 1
}
Show-Ok ('Claude Code: ' + $claude.Source)

# ---------------------------------------------------------------- 3. API key
Show-Step '3/7 API key (must not be used)'
$keyScopes = @()
foreach ($scope in @('Process', 'User', 'Machine')) {
    if ([Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY', $scope)) { $keyScopes += $scope }
}
if ($keyScopes.Count -gt 0) {
    Show-Note ('ANTHROPIC_API_KEY is set (' + ($keyScopes -join ', ') + '). The worker ignores it: it is removed for the worker process only, so nothing is billed to the API.')
    Show-Note 'Your other tools keep the variable. (To remove it everywhere: setx ANTHROPIC_API_KEY "" and delete it in System Properties.)'
    Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
} else {
    Show-Ok 'ANTHROPIC_API_KEY is not set. The worker runs on your subscription.'
}

# ---------------------------------------------------------------- 4. .env
Show-Step '4/7 .env and GITHUB_TOKEN'
$envFile = Join-Path $here '.env'
$example = Join-Path $here '.env.example'
$utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false   # UTF-8 without BOM
if (-not (Test-Path $envFile)) {
    if (-not (Test-Path $example)) { Show-Ng '.env.example is missing. Run this script from the ocr-worker folder.'; exit 1 }
    $seed = [System.IO.File]::ReadAllText($example, $utf8)
    [System.IO.File]::WriteAllText($envFile, $seed, $utf8)
    Show-Ok '.env created from .env.example'
} else {
    Show-Ok '.env already exists (kept)'
}
$text = [System.IO.File]::ReadAllText($envFile, $utf8)
$current = ''
foreach ($line in ($text -split "`r?`n")) {
    if ($line -match '^GITHUB_TOKEN=(.*)$') { $current = $Matches[1].Trim() }
}
$replace = $false
if ($current) {
    Show-Ok 'GITHUB_TOKEN is already set in .env (not shown)'
    $replace = Ask-YesNo 'Replace it with a new token?' $false
} else {
    $replace = $true
}
if ($replace) {
    Write-Host 'Create a fine-grained token at GitHub > Settings > Developer settings > Personal access tokens:'
    Write-Host '  - Repository access: only the Vault repository (obsidian-vault)'
    Write-Host '  - Permissions: Contents = Read and write'
    if ($env:KARTE_SETUP_TOKEN) {
        # Non-interactive use (automation, or a console that cannot do hidden input).
        $token = $env:KARTE_SETUP_TOKEN
    } else {
        $secure = Read-Host 'Paste the token here (nothing is shown while you type)' -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    }
    $token = $token.Trim()
    if (($token.Length -lt 20) -or ($token -match '\s')) {
        Show-Ng 'That does not look like a token (too short, or it contains spaces). Nothing was saved. Run this script again.'
        exit 1
    }
    $lines = @()
    $done = $false
    foreach ($line in ($text -split "`r?`n")) {
        if ($line -match '^GITHUB_TOKEN=') { $lines += ('GITHUB_TOKEN=' + $token); $done = $true } else { $lines += $line }
    }
    if (-not $done) { $lines += ('GITHUB_TOKEN=' + $token) }
    [System.IO.File]::WriteAllText($envFile, ($lines -join "`n"), $utf8)
    $token = $null
    Show-Ok 'GITHUB_TOKEN saved to .env'
}
# Make .env readable only by the current user (it holds a secret).
& icacls $envFile /inheritance:r /grant:r ($env:USERNAME + ':(R,W)') | Out-Null
if ($LASTEXITCODE -eq 0) { Show-Ok '.env is readable only by you' } else { Show-Note 'Could not restrict .env permissions; keep this folder private.' }

# ---------------------------------------------------------------- 5. check
Show-Step '5/7 Checking token, Vault access and Claude login (this calls claude once)'
& python worker.py --check
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Show-Ng 'The check found problems (see [NG] above). Fix them, then run this script again.'
    Show-Note 'Not logged in to Claude Code? Run "claude", type /login, sign in with your subscription account.'
    exit 1
}

# ---------------------------------------------------------------- 6. smoke test
Show-Step '6/7 Run the worker once'
if ($SkipSmokeTest) {
    Show-Note 'Skipped (-SkipSmokeTest).'
} elseif (Ask-YesNo 'Run "python worker.py --once" now? (reads photos that are already waiting, if any)' $true) {
    & python worker.py --once
    if ($LASTEXITCODE -ne 0) { Show-Ng 'worker.py --once failed (see above).'; exit 1 }
    Show-Ok 'Worker ran once without errors.'
} else {
    Show-Note 'Skipped.'
}

# ---------------------------------------------------------------- 7. logon task
Show-Step '7/7 Keep the worker running'
if ($SkipTask) {
    Show-Note 'Skipped (-SkipTask). Start it by hand with: python worker.py'
} elseif (Ask-YesNo 'Register the logon task KarteOcrWorker so the worker starts when you log in?' $true) {
    try {
        & (Join-Path $here 'register_task.ps1')
        Show-Ok 'Task registered and started (log: ocr-worker.log in this folder).'
    } catch {
        Show-Ng ('Could not register the task: ' + $_.Exception.Message)
        Show-Note 'You can still run the worker by hand: python worker.py'
    }
} else {
    Show-Note 'Not registered. Start it by hand with: python worker.py   (or run register_task.ps1 later)'
}

Write-Host ''
Write-Host 'Setup finished.' -ForegroundColor Green
Write-Host 'The karte web app was not changed. Photos you send from the phone are read when this PC is on and logged in.'
