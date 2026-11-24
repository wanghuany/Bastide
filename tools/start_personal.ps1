param([int]$Port = 8899, [switch]$NoBrowser, [string]$Vault, [switch]$KeepRunning)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $repoRoot
$workspacePython = Join-Path $workspaceRoot '.tools\python'
if (Test-Path -LiteralPath $workspacePython) {
    $env:UV_PYTHON_INSTALL_DIR = $workspacePython
}
$backend = Join-Path $repoRoot 'backend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
$env:PYTHONPATH = $backend
$env:PYTHONIOENCODING = 'utf-8'
$env:BASTIDE_DB_PATH = ':memory:'
$ready = $false
if (Test-Path -LiteralPath $python) {
    & $python -c 'import cryptography, mcp, uvicorn, app.personal' 2>$null
    $ready = ($LASTEXITCODE -eq 0)
}
if (-not $ready) {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        throw 'uv is required to prepare Python dependencies. See docs/personal.zh-CN.md.'
    }
    Push-Location $backend
    try {
        & $uv.Source sync --locked --extra personal
        if ($LASTEXITCODE -ne 0) { throw 'Dependency setup failed.' }
    } finally {
        Pop-Location
    }
}
$runArgs = @('-m', 'app.personal_cli', '--port', "$Port")
if ($NoBrowser) { $runArgs += '--no-browser' }
if ($KeepRunning) { $runArgs += '--keep-running' }
if ($Vault) { $runArgs += @('--vault', $Vault) }
Push-Location $backend
try {
    $authArgs = @('-c', 'import sys; from pathlib import Path; from app.features.personal.config import resolve_vault_path; from app.features.personal.machine_access import MachineAccess; sys.exit(0 if MachineAccess(resolve_vault_path(Path(sys.argv[1]) if len(sys.argv)>1 else None)).enabled else 1)')
    if ($Vault) { $authArgs += $Vault }
    & $python @authArgs
    if ($LASTEXITCODE -eq 0) {
        $pythonw = Join-Path $backend '.venv\Scripts\pythonw.exe'
        $quotedArgs = $runArgs | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' }
        Start-Process -FilePath $pythonw -ArgumentList $quotedArgs -WorkingDirectory $backend -WindowStyle Hidden
        exit 0
    }
    & $python @runArgs
    $result = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $result
