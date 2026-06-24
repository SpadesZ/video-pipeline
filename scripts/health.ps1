$ErrorActionPreference = "Stop"

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
  docker compose ps
  Invoke-RestMethod -Uri "http://localhost:8010/health" | Out-Host
}
finally {
  Pop-Location
}

