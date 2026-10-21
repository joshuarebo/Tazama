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
  $py = Get-Command "py" -ErrorAction SilentlyContinue
  if ($py) { return @($py.Source, "-3") }
  throw "Python 3.12+ not found on PATH. Install it, or install uv and run: uv run scripts/mk.py <target>"
}

if (-not $Targets -or $Targets.Count -eq 0) { $Targets = @("help") }

$python = Resolve-Python
$exe = $python[0]
$prefix = @()
if ($python.Count -gt 1) { $prefix = $python[1..($python.Count - 1)] }

& $exe @prefix (Join-Path $root "scripts\mk.py") @Targets
exit $LASTEXITCODE
