# Build dist\compute\compute.exe and dist\compute-windows.zip. Run on Windows from anywhere:
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

function Invoke-Step([string]$Name, [scriptblock]$Command) {
  Write-Host "==> $Name"
  & $Command
  if ($LASTEXITCODE -ne 0) { throw "$Name failed (exit code $LASTEXITCODE)" }
}

Invoke-Step 'uv sync' { uv sync --group dev --group build }
Invoke-Step 'tests' { uv run pytest -q }

Remove-Item -Recurse -Force build\compute, dist\compute -ErrorAction SilentlyContinue
Remove-Item -Force dist\compute-windows.zip -ErrorAction SilentlyContinue
Invoke-Step 'pyinstaller' { uv run pyinstaller --noconfirm --clean --distpath dist --workpath build packaging\compute.spec }

if (-not (Test-Path dist\compute\compute.exe)) { throw 'dist\compute\compute.exe was not produced' }
Compress-Archive -Path dist\compute -DestinationPath dist\compute-windows.zip
Write-Host 'Built dist\compute-windows.zip (contains compute\compute.exe)'
