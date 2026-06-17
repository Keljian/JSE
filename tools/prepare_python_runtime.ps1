$ErrorActionPreference = "Stop"

$pythonVersion = "3.11.9"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$buildDir = Join-Path $root "build"
$cacheDir = Join-Path $buildDir "cache"
$runtimeDir = Join-Path $buildDir "python"
$runtimePython = Join-Path $runtimeDir "python.exe"
$embedZip = Join-Path $cacheDir "python-$pythonVersion-embed-amd64.zip"
$getPip = Join-Path $cacheDir "get-pip.py"
$requirements = Join-Path $root "requirements.txt"

New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null

if (!(Test-Path $embedZip)) {
  $embedUrl = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-embed-amd64.zip"
  Write-Host "Downloading Python $pythonVersion embeddable runtime..."
  Invoke-WebRequest -Uri $embedUrl -OutFile $embedZip
}

if (Test-Path $runtimeDir) {
  Remove-Item -LiteralPath $runtimeDir -Recurse -Force
}

Write-Host "Extracting Python runtime..."
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
Expand-Archive -LiteralPath $embedZip -DestinationPath $runtimeDir -Force

$pthFile = Join-Path $runtimeDir "python311._pth"
@(
  "python311.zip",
  ".",
  "Lib\site-packages",
  "import site"
) | Set-Content -LiteralPath $pthFile -Encoding ASCII

if (!(Test-Path $getPip)) {
  Write-Host "Downloading pip bootstrap..."
  Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip
}

Write-Host "Installing pip into bundled Python..."
$env:PYTHONNOUSERSITE = "1"
& $runtimePython $getPip --no-warn-script-location
if ($LASTEXITCODE -ne 0) {
  throw "pip bootstrap failed with exit code $LASTEXITCODE"
}

Write-Host "Installing Python dependencies into bundled runtime..."
& $runtimePython -m pip install --upgrade --ignore-installed --no-warn-script-location -r $requirements
if ($LASTEXITCODE -ne 0) {
  throw "Python dependency installation failed with exit code $LASTEXITCODE"
}

Write-Host "Verifying bundled Python dependencies..."
& $runtimePython -c "import selenium, openai, google.generativeai, requests, pdfplumber, docx; print('bundled python ok')"
if ($LASTEXITCODE -ne 0) {
  throw "Bundled Python verification failed with exit code $LASTEXITCODE"
}
