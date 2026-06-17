$ErrorActionPreference = "Stop"

Write-Host "Building Job Application Assistant installer..."

$env:CSC_IDENTITY_AUTO_DISCOVERY = "false"

& ".\tools\prepare_python_runtime.ps1"
corepack npm install
corepack npm run dist:win

Write-Host ""
Write-Host "Installer output:"
Get-ChildItem -Path ".\release" -Filter "*.exe" | Select-Object -ExpandProperty FullName
