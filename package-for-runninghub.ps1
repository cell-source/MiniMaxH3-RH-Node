$ErrorActionPreference = "Stop"
python (Join-Path $PSScriptRoot "scripts/package_runninghub.py")
if ($LASTEXITCODE -ne 0) { throw "RunningHub package build failed" }
