# 修复 MaiBotDailyReport 计划任务参数尾引号畸形（"...token_guard.py" --report" → "...token_guard.py" --report）
$path = "C:\Users\29688\Desktop\脚本\QQ-DeepSeek-Bot\token_guard.py"
$action = New-ScheduledTaskAction -Execute "C:\Windows\py.exe" -Argument ('"' + $path + '" --report')
Set-ScheduledTask -TaskName MaiBotDailyReport -Action $action | Out-Null
$t = Get-ScheduledTask -TaskName MaiBotDailyReport
Write-Output ("after: [" + $t.Actions[0].Execute + " " + $t.Actions[0].Arguments + "]")
