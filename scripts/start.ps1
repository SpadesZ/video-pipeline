$ErrorActionPreference = "Stop"

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
  docker compose up -d --build
  Start-Sleep -Seconds 3
  Invoke-RestMethod -Uri "http://localhost:8010/health" | Out-Host
  Write-Host "Video Pipeline is running at http://localhost:8010/"
}
finally {
  Pop-Location
}

