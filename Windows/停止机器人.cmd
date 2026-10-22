@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
if not exist ".venv\Scripts\python.exe" (
  echo 尚未安装本项目运行环境。
  pause
  exit /b 0
)
".venv\Scripts\python.exe" scripts\control.py stop
if errorlevel 1 goto failed
exit /b 0
:failed
echo 操作未完成，请查看上方提示或 logs\server.log。
pause
exit /b 1
