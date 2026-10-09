@echo off
rem tsov one-click launcher. MUST keep CRLF line endings - cmd shreds LF-only bats.
rem Do NOT re-save this file with Unix/LF endings.
rem The Chinese hint below is intentional - this branch runs when python-base is
rem missing, so the message cannot come from bootstrap.py.
setlocal
title tsov One-Click Bundle
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0python-base\python.exe" (
  echo [ERROR] python-base\python.exe not found.
  echo Please extract the WHOLE folder first, then run the launcher again.
  echo 请先完整解压整个文件夹，再双击本启动器（详见 使用说明.txt）。
  pause
  exit /b 1
)
"%~dp0python-base\python.exe" "%~dp0bootstrap.py" %*
echo.
echo tsov stopped. You can close this window now.
pause
