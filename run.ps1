param([switch]$PrepareOnly, [switch]$SmokeTest)

$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
$taskVersions = '-3.13', '-3.12', '-3.11'
$taskModules = "'numpy','cv2','PIL'"
$taskRequirements = 'requirements-forever.txt'
if (Test-Path -LiteralPath (Join-Path $taskRoot 'assets/classic_arrow.png')) {
    $taskVersions = '-3.12', '-3.11'
    $taskModules += ",'rapidocr_onnxruntime'"
    $taskRequirements = 'requirements.txt'
}
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    $taskFirstVersion = $taskVersions[0].TrimStart('-')
    if (-not $taskLauncher) { throw "Install 64-bit Python $taskFirstVersion for Windows with the Python launcher." }
    $taskVersion = $null
    foreach ($taskCandidate in $taskVersions) {
        & $taskLauncher.Source $taskCandidate -c "import struct, tkinter; raise SystemExit(struct.calcsize('P') != 8)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $taskVersion = $taskCandidate
            break
        }
    }
    if (-not $taskVersion) { throw "Install 64-bit Python $taskFirstVersion for Windows with Tcl/Tk." }
    & $taskLauncher.Source $taskVersion -m venv (Join-Path $taskRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Route Me Python environment.' }
}
& $taskPython -c "import importlib.util,sys; sys.exit(0 if all(importlib.util.find_spec(m) for m in ($taskModules)) else 1)"
if ($LASTEXITCODE -ne 0) {
    & $taskPython -m pip install -r (Join-Path $taskRoot $taskRequirements)
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
if ($PrepareOnly) { return }
$taskArguments = @((Join-Path $taskRoot 'app.py'))
if ($SmokeTest) { $taskArguments += '--smoke-test' }
& $taskPython @taskArguments
exit $LASTEXITCODE
