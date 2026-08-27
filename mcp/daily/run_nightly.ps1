# JSE nightly loop. Runs unattended, costs no Claude tokens.
#
#   scrape:run  ->  analysis:run  ->  jobs:exportShortlist  ->  prefilter.py
#
# Each step is a fresh python_bridge.py process, the same way Electron runs the
# cancellable tasks, so one wedged step cannot take the rest down. Every step
# writes its raw protocol frames to logs\ and a summary line to nightly_status.json,
# which is what jse_nightly_status reads in the morning.
#
#   powershell -ExecutionPolicy Bypass -File C:\JSE\mcp\daily\run_nightly.ps1
#   ... -SkipScrape        reuse last night's rows, just re-analyse and export
#   ... -SkipAnalysis      export from what is already scored

param(
    [switch]$SkipScrape,
    [switch]$SkipAnalysis,
    [int]$ScrapeTimeoutMinutes = 240,
    [int]$AnalysisTimeoutMinutes = 420,
    [int]$ExportTimeoutMinutes = 30
)

$ErrorActionPreference = "Continue"
$Root   = "C:\JSE"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Bridge = Join-Path $Root "python_bridge.py"
$Daily  = Join-Path $Root "mcp\daily"
$Logs   = Join-Path $Daily "logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

$RunStamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$Steps = @()

function Write-Status {
    $payload = [ordered]@{
        run          = $RunStamp
        started_at   = $script:StartedAt
        finished_at  = (Get-Date).ToString("s")
        host         = [System.Net.Dns]::GetHostName()
        steps        = $Steps
    }
    $payload | ConvertTo-Json -Depth 6 | Set-Content -Path (Join-Path $Daily "nightly_status.json") -Encoding UTF8
}

function Invoke-BridgeStep {
    param([string]$Name, [string]$Command, [string]$PayloadJson, [int]$TimeoutMinutes)

    $started = Get-Date
    Write-Host "[$($started.ToString('HH:mm:ss'))] $Name : $Command"

    $payloadFile = Join-Path $Logs "$RunStamp.$Name.payload.json"
    $outFile     = Join-Path $Logs "$RunStamp.$Name.log"
    $errFile     = Join-Path $Logs "$RunStamp.$Name.err"
    [IO.File]::WriteAllText($payloadFile, $PayloadJson)

    $proc = Start-Process -FilePath $Python -ArgumentList $Bridge, $Command `
        -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
        -RedirectStandardInput $payloadFile -RedirectStandardOutput $outFile -RedirectStandardError $errFile

    $timedOut = $false
    if (-not $proc.WaitForExit($TimeoutMinutes * 60 * 1000)) {
        $timedOut = $true
        try { $proc.Kill() } catch {}
        Start-Sleep -Seconds 5
    }

    # The bridge's last protocol frame is the outcome. Anything else on stdout is
    # progress, which is only interesting when something went wrong.
    $result = $null; $errorMessage = $null
    if (Test-Path $outFile) {
        foreach ($line in (Get-Content $outFile -ErrorAction SilentlyContinue)) {
            if ($line -notmatch '^\{') { continue }
            try { $frame = $line | ConvertFrom-Json } catch { continue }
            if ($frame.type -eq "result") { $result = $frame.data }
            elseif ($frame.type -eq "error") { $errorMessage = $frame.message }
        }
    }

    # WaitForExit(ms) does not populate ExitCode; the parameterless call after it
    # does. Without this every step reports a null exit code and looks like a failure.
    if (-not $timedOut) { try { $proc.WaitForExit() } catch {} }
    $exitCode = $null
    try { $exitCode = $proc.ExitCode } catch {}
    $elapsed = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
    $ok = (-not $timedOut) -and ($null -eq $errorMessage) -and (($exitCode -eq 0) -or ($null -ne $result))
    $script:Steps += [ordered]@{
        name            = $Name
        command         = $Command
        ok              = $ok
        timed_out       = $timedOut
        exit_code       = $exitCode
        minutes         = $elapsed
        error           = $errorMessage
        log             = $outFile
    }
    Write-Status
    if (-not $ok) { Write-Host "  ! $Name failed: $errorMessage (exit $exitCode, timeout=$timedOut)" }
    else { Write-Host "  ok in $elapsed min" }
    return $ok
}

# A stray analysis process from an earlier manual run will contend with this one
# for the single local endpoint and halve both. Only analysis:run is matched, so
# the Electron worker and any scrape are left alone.
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object {
    $_.CommandLine -like "*python_bridge.py*analysis:run*"
} | ForEach-Object {
    Write-Host "stopping stray analysis process $($_.ProcessId)"
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
}
$script:StartedAt = (Get-Date).ToString("s")
Write-Status

# Two seconds of checking beats four hours of a run that was never going to score
# anything. If the local endpoint is down or serving a different model, analysis is
# skipped and the reason is recorded, so the morning brief explains itself.
$preflightOut = & $Python (Join-Path $Daily "preflight.py") 2>&1 | Out-String
$preflightOk = $LASTEXITCODE -eq 0
$preflight = $null
try { $preflight = $preflightOut | ConvertFrom-Json } catch {}
$Steps += [ordered]@{
    name    = "preflight"
    command = "preflight.py"
    ok      = $preflightOk
    minutes = 0
    error   = $(if ($preflightOk) { $null } else { ($preflight.problems -join "; ") })
    detail  = $preflight
}
Write-Status
if (-not $preflightOk) {
    Write-Host "  ! preflight failed: $($preflight.problems -join '; ')"
    $SkipAnalysis = $true
}
$allLanes = '{"profile_id":1,"include_all_profiles":true}'

if (-not $SkipScrape) {
    Invoke-BridgeStep -Name "scrape" -Command "scrape:run" -PayloadJson $allLanes -TimeoutMinutes $ScrapeTimeoutMinutes | Out-Null
} else {
    Write-Host "scrape skipped"
}

if (-not $SkipAnalysis) {
    # Every unscored `new` row, no cap. Unattended, so there is no reason to stop
    # early; a partial pass is what produced the 14%-coverage packets.
    Invoke-BridgeStep -Name "analysis" -Command "analysis:run" `
        -PayloadJson '{"profile_id":1,"include_all_profiles":true,"stage":"new"}' `
        -TimeoutMinutes $AnalysisTimeoutMinutes | Out-Null
} else {
    Write-Host "analysis skipped"
}

Invoke-BridgeStep -Name "export" -Command "jobs:exportShortlist" `
    -PayloadJson '{"profile_id":1,"include_all_profiles":true,"format":"both"}' `
    -TimeoutMinutes $ExportTimeoutMinutes | Out-Null

# The prefilter is the whole point: it turns the multi-megabyte packet into the
# few kilobytes the morning decision actually needs.
$briefStarted = Get-Date
$briefOut = & $Python (Join-Path $Daily "prefilter.py") 2>&1 | Out-String
$briefOk = $LASTEXITCODE -eq 0
$summary = $null
foreach ($line in ($briefOut -split "`n")) {
    if ($line -match '^\{') { try { $summary = $line | ConvertFrom-Json } catch {} }
}
$Steps += [ordered]@{
    name    = "brief"
    command = "prefilter.py"
    ok      = $briefOk
    minutes = [math]::Round(((Get-Date) - $briefStarted).TotalMinutes, 1)
    error   = $(if ($briefOk) { $null } else { $briefOut.Trim() })
    summary = $summary
}
Write-Status

Write-Host ""
Write-Host "Nightly run $RunStamp finished. Status: $(Join-Path $Daily 'nightly_status.json')"
if ($summary) {
    Write-Host "Brief: $($summary.brief_md)  ($($summary.after_filters) of $($summary.packet_rows) rows, $($summary.scored_pct)% scored)"
}
