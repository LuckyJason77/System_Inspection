@echo off
if "%INSPECTION_SCHEDULER_UTF8%"=="1" goto utf8_ready
set "INSPECTION_SCHEDULER_HAS_ARGS=0"
if not "%~1"=="" set "INSPECTION_SCHEDULER_HAS_ARGS=1"
chcp 65001 >nul
set "INSPECTION_SCHEDULER_UTF8=1"
"%ComSpec%" /d /c ""%~f0""
exit /b %errorlevel%

:utf8_ready
setlocal
title 自动化巡检调度服务

if "%INSPECTION_SCHEDULER_HAS_ARGS%"=="1" (
    echo 启动失败：该 BAT 不接受启动参数。
    pause
    exit /b 2
)

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 启动失败：未找到虚拟环境 Python：.venv\Scripts\python.exe
    echo 请先创建虚拟环境并安装项目依赖。
    pause
    exit /b 2
)

if not exist "scheduler_service.py" (
    echo 启动失败：未找到调度脚本 scheduler_service.py
    pause
    exit /b 2
)

".venv\Scripts\python.exe" "scheduler_service.py"
set "exit_code=%errorlevel%"

echo.
echo 调度服务已结束，退出码: %exit_code%
pause
exit /b %exit_code%
