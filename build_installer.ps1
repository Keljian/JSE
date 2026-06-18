$ErrorActionPreference = "Stop"

Write-Host "Building JSE unsigned Windows beta installer..."

$env:CSC_IDENTITY_AUTO_DISCOVERY = "false"

& ".\tools\prepare_python_runtime.ps1"
corepack npm ci
corepack npm run dist:win

Write-Host ""
Write-Host "Installer output:"
$installers = Get-ChildItem -Path ".\release" -Filter "*unsigned-beta.exe"
$installers | Select-Object -ExpandProperty FullName
Write-Host ""
Write-Host "SHA-256 checksums (publish these with the beta):"
$installers | Get-FileHash -Algorithm SHA256 | Format-Table Hash, Path -AutoSize
