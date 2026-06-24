$ErrorActionPreference = "Stop"

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
  docker compose down
}
finally {
  Pop-Location
}

