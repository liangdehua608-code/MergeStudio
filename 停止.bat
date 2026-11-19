@echo off
chcp 936 >nul
title 停止 视频拼接工坊
cd /d "%~dp0"

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo 没有找到 Python，无法执行停止。
  pause
  exit /b 1
)

echo 正在停止服务……
echo 如有任务在跑，会先取消它并等 ffmpeg 退出。
echo.

%PY% "run.py" --stop
echo.
pause
