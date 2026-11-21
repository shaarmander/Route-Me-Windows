param([string]$Client, [string]$DataRoot, [switch]$GenerateMeshes, [switch]$NoShortcut)

$ErrorActionPreference = 'Stop'
if (-not $Client) {
    $Client = (Read-Host 'Enter the original WoW 1.12.1 folder containing WoW.exe and Data').Trim().Trim('"')
}
if (-not $Client) { throw 'Select the original World of Warcraft 1.12.1 folder.' }
$Client = (Resolve-Path -LiteralPath $Client).Path
$routeExe = Get-Item -LiteralPath (Join-Path $Client 'WoW.exe') -ErrorAction SilentlyContinue
if (-not $routeExe -or $routeExe.VersionInfo.FilePrivatePart -ne 5875) {
    throw 'This setup requires the original WoW 1.12.1 client, build 5875.'
}
if ($DataRoot -and $GenerateMeshes) { throw 'Choose either an existing DataRoot or GenerateMeshes.' }

& (Join-Path $PSScriptRoot 'run.ps1') -PrepareOnly
$routePython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
& $routePython -c "import importlib.util,sys; sys.exit(importlib.util.find_spec('mpyq') is None)"
if ($LASTEXITCODE -ne 0) {
    & $routePython -m pip install 'mpyq==0.2.5'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the MPQ reader.' }
}

$routeArgs = @('-m', 'tools.setup_classic', $Client)
if ($DataRoot) { $routeArgs += '--data-root', (Resolve-Path -LiteralPath $DataRoot).Path }
if ($GenerateMeshes) { $routeArgs += '--generate-meshes' }
Push-Location -LiteralPath $PSScriptRoot
try {
    & $routePython @routeArgs
    if ($LASTEXITCODE -ne 0) { throw 'Classic setup stopped. See the message above.' }
} finally {
    Pop-Location
}
if (-not $NoShortcut) { & (Join-Path $PSScriptRoot 'Install Start Menu.ps1') }
Write-Host 'Open Route Me, select an outdoor zone, and use Draw trip to preview collision-checked paths.'
