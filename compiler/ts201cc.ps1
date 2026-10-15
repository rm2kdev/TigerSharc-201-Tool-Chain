$ErrorActionPreference = 'Stop'
$interpreter = $env:TS201_PYTHON
if (-not $interpreter) {
    # Skip the Microsoft Store placeholder (WindowsApps python.exe), which is not a real Python.
    $command = Get-Command python -All -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike "*WindowsApps*" } | Select-Object -First 1
    if ($command) { $interpreter = $command.Source }
}
if (-not $interpreter) {
    $candidate = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
    if (Test-Path -LiteralPath $candidate) { $interpreter = $candidate }
}
if (-not $interpreter) { throw 'Python 3.10+ required. Set TS201_PYTHON to its executable path.' }
& $interpreter (Join-Path $PSScriptRoot 'ts201cc.py') @args
exit $LASTEXITCODE
