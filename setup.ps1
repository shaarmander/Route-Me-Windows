param([string]$Client, [switch]$NoShortcut)

$ErrorActionPreference = 'Stop'
$routeRuntime = Join-Path $PSScriptRoot 'runtime'
if (-not (Test-Path -LiteralPath (Join-Path $routeRuntime 'Db2Export.exe'))) {
    throw 'Download and extract the Windows ZIP from GitHub Releases before running setup.'
}

if (-not $Client) {
    $routeCandidates = @(
        Get-Process -Name Wow -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.Path -and (Split-Path (Split-Path $_.Path) -Leaf) -eq '_forever_') {
                Split-Path (Split-Path $_.Path)
            }
        }
        Join-Path ${env:ProgramFiles(x86)} 'World of Warcraft'
        Join-Path $env:ProgramFiles 'World of Warcraft'
    )
    $Client = $routeCandidates | Where-Object {
        (Test-Path -LiteralPath (Join-Path $_ '_forever_/Wow.exe') -PathType Leaf)
    } | Select-Object -First 1
    if (-not $Client) {
        $Client = (Read-Host 'Enter the World of Warcraft folder that contains _forever_').Trim().Trim('"')
    }
}
if (-not $Client -or -not (Test-Path -LiteralPath (Join-Path $Client '_forever_/Wow.exe') -PathType Leaf)) {
    throw 'Choose the World of Warcraft folder that contains _forever_/Wow.exe.'
}
$Client = (Resolve-Path -LiteralPath $Client).Path

& (Join-Path $PSScriptRoot 'run.ps1') -PrepareOnly
$routePython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
Push-Location -LiteralPath $PSScriptRoot
try {
    & $routePython -m tools.setup_forever $Client
    if ($LASTEXITCODE -ne 0) { throw 'The WoW: Forever map import did not finish. Review the error above, then rerun setup.' }
} finally {
    Pop-Location
}

$routeAddon = Join-Path $Client '_forever_/Interface/AddOns/Wayfinder'
try {
    New-Item -ItemType Directory -Path $routeAddon -Force | Out-Null
    foreach ($routeFile in 'Wayfinder.lua', 'Wayfinder.toc') {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot "addon/Wayfinder/$routeFile") -Destination $routeAddon
    }
    Write-Host 'Installed Wayfinder. Restart WoW, log in, and enable Wayfinder on the AddOns list.'
} catch {
    Write-Host 'Windows blocked copying the addon. Copy addon/Wayfinder into the Forever client AddOns folder.'
    Write-Host "Destination folder: $routeAddon"
}

if (-not $NoShortcut) { & (Join-Path $PSScriptRoot 'Install Start Menu.ps1') }
Write-Host 'Setup complete. Open Route Me from the Start menu or run Launch Route Me.cmd.'
