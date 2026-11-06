<#
.SYNOPSIS
  TAZAMA task runner for Windows, where GNU make is usually missing.
.EXAMPLE
  ./make.ps1 doctor
  ./make.ps1 ci
  ./make.ps1 demo
#>
param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$Targets
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Resolve-Python {
  foreach ($candidate in @("python", "python3")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { return @($cmd.Source) }
  }
  $venvPy = Join-Path $root "backend\.venv\Scripts\python.exe"
  if (Test-Path $venvPy) { return @($venvPy) }
  $py = Get-Command "py" -ErrorAction SilentlyContinue
  if ($py) {
    try {
      $null = & $py.Source -3 -c "import sys" 2>&1
      if ($LASTEXITCODE -eq 0) { return @($py.Source, "-3") }
    } catch {}
  }
  throw "Python 3.12+ not found on PATH. Install it, or install uv and run: uv run scripts/mk.py <target>"
}

if (-not $Targets -or $Targets.Count -eq 0) { $Targets = @("help") }

[string[]]$python = @(Resolve-Python)
$exe = $python[0]
$prefix = @()
if ($python.Count -gt 1) { $prefix = $python[1..($python.Count - 1)] }

& $exe @prefix (Join-Path $root "scripts\mk.py") @Targets
exit $LASTEXITCODE
