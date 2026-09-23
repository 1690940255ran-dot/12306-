@echo off
rem RailAssist 桌面端启动器（使用 .runtime\prod 数据目录：已登记能力与会话）
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo [RailAssist] 未找到虚拟环境，请先运行安装命令：python -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -e ".[browser,gui]"
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import railassist, PySide6, playwright" >nul 2>&1
if errorlevel 1 (
  echo [RailAssist] 虚拟环境已损坏或依赖不完整。请删除 .venv 后重新执行：
  echo python -m venv .venv
  echo .venv\Scripts\python.exe -m pip install -e ".[browser,gui]"
  echo .venv\Scripts\python.exe -m playwright install chromium
  pause
  exit /b 1
)
start "RailAssist" ".venv\Scripts\pythonw.exe" -m railassist --data-dir "%~dp0.runtime\prod" gui
