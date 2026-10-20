# 매일 23:50 에 tools/daily.py 를 실행하는 Windows 작업 스케줄러 항목을 등록/갱신한다.
# 실행: powershell -ExecutionPolicy Bypass -File tools\install_task.ps1
$ErrorActionPreference = "Stop"
$taskName = "DiaryTimelapse"
$script   = Join-Path $PSScriptRoot "daily.py"
$repoDir  = Split-Path $PSScriptRoot
$pythonw  = Join-Path (Split-Path (Get-Command python).Source) "pythonw.exe"   # 콘솔창 없이 실행
if (-not (Test-Path $pythonw)) { $pythonw = (Get-Command python).Source }

$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $repoDir
$trigger  = New-ScheduledTaskTrigger -Daily -At 23:50
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
            -MultipleInstances IgnoreNew -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State | Format-Table -AutoSize
"python : $pythonw"
"script : $script"
