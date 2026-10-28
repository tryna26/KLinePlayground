param(
    [string]$DataDir,
    [string]$UsersDir,
    [int]$Port = 5050,
    [switch]$Desktop,
    [switch]$PortfolioDemo
)
$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw '请先按 docs/DEVELOPMENT.md 创建 .venv 并安装 requirements.txt。'
}
$launchArgs = @((Join-Path $PSScriptRoot 'run_dev.py'), '--port', $Port)
if ($DataDir) { $launchArgs += @('--data-dir', $DataDir) }
if ($UsersDir) { $launchArgs += @('--users-dir', $UsersDir) }
if ($Desktop) { $launchArgs += '--desktop' }
if ($PortfolioDemo) { $launchArgs += '--portfolio-demo' }
& $pythonPath @launchArgs
exit $LASTEXITCODE
