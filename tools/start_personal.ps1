param([int]$Port = 8899, [switch]$NoBrowser, [string]$Vault)
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
if ($Vault) { $runArgs += @('--vault', $Vault) }
Push-Location $backend
try {
    & $python @runArgs
    $result = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $result
