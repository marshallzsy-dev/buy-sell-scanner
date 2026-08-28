@echo off
REM 注册每日自动运行的 Windows 计划任务（默认本机 08:00，美股已收盘）。
REM 想改时间：编辑下面的 /st 08:00。想严格按美东中午12点：改成 /st 00:00（夏令时）。
REM 卸载：schtasks /delete /tn "S1DailyScan" /f

set "TASKCMD=\"%~dp0run.bat\""
schtasks /create /tn "S1DailyScan" /tr "%TASKCMD%" /sc daily /st 08:00 /f
echo.
echo 已注册任务 S1DailyScan。查询: schtasks /query /tn "S1DailyScan"
echo 立即测试: schtasks /run /tn "S1DailyScan"
pause
