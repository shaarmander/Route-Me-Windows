$ErrorActionPreference = 'Stop'
$routePython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $routePython)) {
    throw 'Run Launch Route Me.cmd once to set up Python, then install the shortcut again.'
}
$routePrograms = [Environment]::GetFolderPath('Programs')
$routeLink = Join-Path $routePrograms 'Route Me.lnk'
$routeShell = New-Object -ComObject WScript.Shell
$routeShortcut = $routeShell.CreateShortcut($routeLink)
$routeShortcut.TargetPath = $routePython
$routeShortcut.Arguments = '"' + (Join-Path $PSScriptRoot 'app.py') + '"'
$routeShortcut.WorkingDirectory = $PSScriptRoot
$routeShortcut.Description = 'Open the Route Me map and route planner'
$routeShortcut.IconLocation = $routePython + ',0'
$routeShortcut.Save()
Write-Output "Installed Start menu shortcut: $routeLink"
