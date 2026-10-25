$procs = Get-CimInstance Win32_Process | Where-Object { ($_.Name -match 'python|maibot') -and ($_.CommandLine -match 'maibot') -and ($_.ProcessId -ne $PID) }
foreach ($p in $procs) {
  Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
  Write-Output ("KILLED {0} {1}" -f $p.ProcessId, $p.Name)
}
Start-Sleep -Seconds 2
$left = (Get-CimInstance Win32_Process | Where-Object { ($_.Name -match 'python|maibot') -and ($_.CommandLine -match 'maibot') -and ($_.ProcessId -ne $PID) } | Measure-Object).Count
Write-Output ("LEFT: {0}" -f $left)
