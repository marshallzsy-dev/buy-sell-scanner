@echo off
REM ── S1 每日买卖点扫描 ──
REM 双击可手动运行；也被 Windows 任务计划程序每日调用。
setlocal

cd /d "%~dp0"

REM 优先用实际的 Python 解释器（任务计划下 WindowsApps 别名可能失效）
set "PY=C:\Users\shuyongzhang\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=python"

echo [%date% %time%] 开始扫描...
"%PY%" "%~dp0scan.py" >> "%~dp0run.log" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] 结束 (exit=%RC%)  详见 run.log

REM 运行成功后自动用默认浏览器打开结果（任务计划静默运行时此步无害）
if %RC%==0 start "" "%~dp0dashboard.html"

endlocal
exit /b %RC%
