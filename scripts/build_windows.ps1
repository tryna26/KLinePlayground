param(
    [string]$Python = "$PSScriptRoot\..\.venv\Scripts\python.exe",
    [string]$Iscc = $env:ISCC_EXE
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location -LiteralPath $projectRoot
if (-not $Iscc) {
    $compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($compiler) { $Iscc = $compiler.Source }
}
if (-not $Iscc) {
    foreach ($candidate in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
        if (Test-Path -LiteralPath $candidate) { $Iscc = $candidate; break }
    }
}
if (-not $Iscc -or -not (Test-Path -LiteralPath $Iscc)) { throw 'Specify Inno Setup 6.7.3 compiler with -Iscc or ISCC_EXE.' }
& $Python -m pip check
if ($LASTEXITCODE) { throw 'Dependency check failed.' }
& $Python scripts/collect_licenses.py
if ($LASTEXITCODE) { throw 'License collection failed.' }
& $Python -m PyInstaller --noconfirm --clean packaging/windows.spec
if ($LASTEXITCODE) { throw 'PyInstaller build failed.' }
& $Iscc packaging/windows.iss
if ($LASTEXITCODE) { throw 'Installer build failed.' }
& $Python scripts/package_release.py
if ($LASTEXITCODE) { throw 'Release packaging failed.' }
