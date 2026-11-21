param([Parameter(Mandatory=$true)][string]$DetourRoot)
$ErrorActionPreference = 'Stop'
$buildDir = Join-Path $PSScriptRoot 'native\build'
& cmake -S (Join-Path $PSScriptRoot 'native') -B $buildDir -G 'Visual Studio 17 2022' -A x64 "-DDETOUR_ROOT=$DetourRoot"
if ($LASTEXITCODE) { throw 'Navigation configure failed' }
& cmake --build $buildDir --config Release
if ($LASTEXITCODE) { throw 'Navigation build failed' }
