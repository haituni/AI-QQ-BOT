@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
if exist ".venv\Scripts\python.exe" goto ready
where py >nul 2>nul
if errorlevel 1 (
  echo 请先安装 Python 3.12 x64，包含 Python Launcher，然后重新双击此文件。
  pause
  exit /b 1
)
py -3 -X utf8 scripts\bootstrap.py
if errorlevel 1 goto failed
:ready
".venv\Scripts\python.exe" scripts\control.py start
if errorlevel 1 goto failed
exit /b 0
:failed
echo 操作未完成，请查看上方提示或 logs\server.log。
pause
exit /b 1
