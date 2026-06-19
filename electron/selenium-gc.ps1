param(
  [switch]$Report
)

# Reap Selenium trees only when their owning Python process has exited. Normal
# Chrome profiles are never considered: Selenium's scoped_dir<driver-pid>_
# profile convention is required before a standalone Chrome process is removed.
$ErrorActionPreference = "Stop"
$orphanDriverCount = 0
$treeProcessCount = 0
$strayChromeCount = 0

try {
  $all = @(Get-CimInstance Win32_Process)
  $processById = @{}
  foreach ($process in $all) {
    $processById[[int]$process.ProcessId] = $process
  }

  $orphanDrivers = @($all | Where-Object {
    if ($_.Name -ne "chromedriver.exe") { return $false }
    $parentId = [int]$_.ParentProcessId
    if (-not $processById.ContainsKey($parentId)) { return $true }
    # ParentProcessId can be recycled after the real parent exits. A parent
    # created after ChromeDriver cannot be ChromeDriver's actual owner.
    return $processById[$parentId].CreationDate -gt $_.CreationDate
  })
  $orphanDriverCount = $orphanDrivers.Count

  if ($orphanDriverCount -gt 0) {
    $killIds = [System.Collections.Generic.HashSet[int]]::new()
    $queue = [System.Collections.Generic.Queue[int]]::new()
    foreach ($driver in $orphanDrivers) {
      $queue.Enqueue([int]$driver.ProcessId)
    }

    while ($queue.Count -gt 0) {
      $parentId = $queue.Dequeue()
      if (-not $killIds.Add($parentId)) { continue }
      foreach ($child in $all) {
        if ([int]$child.ParentProcessId -eq $parentId) {
          $queue.Enqueue([int]$child.ProcessId)
        }
      }
    }

    $killIdList = @($killIds)
    $treeProcessCount = $killIdList.Count
    Stop-Process -Id $killIdList -Force -ErrorAction SilentlyContinue
  }

  Start-Sleep -Milliseconds 250
  $remaining = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match "^(chrome|chromedriver)\.exe$"
  })
  $driverIds = [System.Collections.Generic.HashSet[int]]::new()
  foreach ($driver in ($remaining | Where-Object { $_.Name -eq "chromedriver.exe" })) {
    [void]$driverIds.Add([int]$driver.ProcessId)
  }

  $strayChrome = @()
  foreach ($process in ($remaining | Where-Object { $_.Name -eq "chrome.exe" })) {
    if ($process.CommandLine -match "scoped_dir(\d+)_") {
      $ownerDriverId = [int]$Matches[1]
      if (-not $driverIds.Contains($ownerDriverId)) {
        $strayChrome += $process
      }
    }
  }

  $strayChromeCount = $strayChrome.Count
  if ($strayChromeCount -gt 0) {
    Stop-Process -Id @($strayChrome.ProcessId) -Force -ErrorAction SilentlyContinue
  }
} catch {
  # Cleanup is best-effort and must never interrupt the application.
  if ($Report) {
    [pscustomobject]@{ error = $_.Exception.Message } | ConvertTo-Json -Compress
  }
  exit 0
}

if ($Report) {
  [pscustomobject]@{
    orphan_drivers = $orphanDriverCount
    tree_processes = $treeProcessCount
    stray_chrome = $strayChromeCount
  } | ConvertTo-Json -Compress
}
