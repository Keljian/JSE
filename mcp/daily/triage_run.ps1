# Start the local model, wait for it, wait for any scrape to clear, then triage.
#
# The scan and the scoring are separate halves of the pipeline and only the scan
# has been running. This closes the other half on demand: bring Unsloth Studio up,
# wait until it actually answers, let any in-flight scrape finish so the two are
# not fighting over one GPU, then run the tested chain from run_nightly.ps1
# (preflight -> analysis -> export -> prefilter).
#
#   powershell -ExecutionPolicy Bypass -File C:\JSE\mcp\daily\triage_run.ps1

param(
    [int]$EndpointWaitMinutes = 20,
    [int]$ScrapeWaitMinutes   = 120
)

$ErrorActionPreference = "Continue"
$Daily  = "C:\JSE\mcp\daily"
$Logs   = Join-Path $Daily "logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
$Log = Join-Path $Logs ("triage_" + (Get-Date -Format "yyyy-MM-dd_HHmm") + ".log")

function Say($m) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    Write-Host $line
    Add-Content -Path $Log -Value $line
}

Say "triage run starting"

# ---- 1. Model server --------------------------------------------------------
# Which server to start, if any, comes from automation.json beside this script
# (not tracked: it holds machine paths). {"server_exe": "...", "server_process": "..."}
# With no file, nothing is started and step 2 just waits for the endpoint.
$automation = Join-Path $Daily "automation.json"
$serverExe = $null; $serverProcess = $null; $serverArgs = $null
if (Test-Path $automation) {
    try {
        $auto = Get-Content $automation -Raw | ConvertFrom-Json
        $serverExe = $auto.server_exe; $serverProcess = $auto.server_process; $serverArgs = $auto.server_args
    } catch { Say "! could not read $automation" }
}
if ($serverExe) {
    $running = $null
    if ($serverProcess) {
        $running = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match $serverProcess }
    }
    if ($running) {
        Say "model server already running (pid $($running[0].Id))"
    } elseif (Test-Path $serverExe) {
        Say "starting model server: $serverExe"
        if ($serverArgs) { Start-Process -FilePath $serverExe -ArgumentList $serverArgs -WindowStyle Minimized }
        else { Start-Process -FilePath $serverExe -WindowStyle Minimized }
    } else {
        Say "! model server not found at $serverExe"
    }
} else {
    Say "no model server configured in automation.json; waiting for the endpoint"
}

# ---- 2. Wait for the endpoint to answer ------------------------------------
# Reading the key at runtime rather than baking it into this file.
$settingsPath = "C:\JSE\settings\local_llm_settings.json"
$base = "http://localhost:8888/v1"; $key = ""
if (Test-Path $settingsPath) {
    $cfg  = Get-Content $settingsPath -Raw | ConvertFrom-Json
    if ($cfg.local_base_url) { $base = $cfg.local_base_url.TrimEnd('/') }
    if ($cfg.local_api_key)  { $key  = $cfg.local_api_key }
}

$deadline = (Get-Date).AddMinutes($EndpointWaitMinutes)
$endpointUp = $false
while ((Get-Date) -lt $deadline) {
    try {
        $headers = @{}
        if ($key) { $headers["Authorization"] = "Bearer $key" }
        $r = Invoke-WebRequest -Uri "$base/models" -Headers $headers -TimeoutSec 10 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $endpointUp = $true; break }
    } catch { }
    Start-Sleep -Seconds 15
}
if ($endpointUp) {
    Say "local endpoint answering at $base"
} else {
    Say "! endpoint did not come up within $EndpointWaitMinutes min."
    Say "! the model server may need starting by hand (see automation.json). Skipping analysis."
    Say "triage run aborted"
    exit 1
}

# ---- 3. Let any running scrape finish --------------------------------------
# Two heavy jobs against one local endpoint halve both; this is the contention
# that made a nightly run look like it was taking 2.4 minutes a row.
$deadline = (Get-Date).AddMinutes($ScrapeWaitMinutes)
while ((Get-Date) -lt $deadline) {
    $scrape = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*python_bridge.py*scrape:run*" }
    if (-not $scrape) { break }
    Say "scrape still running (pid $($scrape[0].ProcessId)); waiting"
    Start-Sleep -Seconds 120
}
$scrape = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*python_bridge.py*scrape:run*" }
if ($scrape) {
    Say "! scrape still running after $ScrapeWaitMinutes min; proceeding anyway"
} else {
    Say "no scrape in flight"
}

# ---- 4. The tested chain, minus the scrape ---------------------------------
Say "handing off to run_nightly.ps1 -SkipScrape"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Daily "run_nightly.ps1") -SkipScrape *>&1 |
    Tee-Object -FilePath $Log -Append

Say "triage run finished"
