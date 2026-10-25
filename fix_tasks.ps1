# 修复 MaiBotDailyLife / MaiBotQzonePoster 计划任务参数引号丢失问题
$fixes = @(
    @{ Name = "MaiBotDailyLife";  Script = "daily_life.py" },
    @{ Name = "MaiBotQzonePoster"; Script = "qzone_poster.py" }
)
foreach ($f in $fixes) {
    $path = "C:\Users\29688\Desktop\脚本\QQ-DeepSeek-Bot\" + $f.Script
    $action = New-ScheduledTaskAction -Execute "C:\Windows\py.exe" -Argument ('"' + $path + '"')
    Set-ScheduledTask -TaskName $f.Name -Action $action | Out-Null
}
Get-ScheduledTask MaiBotDailyLife, MaiBotQzonePoster | ForEach-Object {
    Write-Output ($_.TaskName + " => [" + ($_.Actions | ForEach-Object { $_.Execute + " " + $_.Arguments }) + "]")
}
