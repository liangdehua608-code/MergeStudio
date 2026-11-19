@echo off
chcp 936 >nul
title 视频拼接工坊 MergeStudio
cd /d "%~dp0"

if not exist "run.py" (
  echo 找不到 run.py，请确认本文件与项目在同一目录。
  pause
  exit /b 1
)

rem 优先使用项目自带 venv，其次 py 启动器，最后 PATH 里的 python
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
if not defined PY goto nopython

echo 解释器：%PY%
echo 正在启动，服务窗口请保持打开……
echo.

%PY% "run.py" %*
set "RC=%ERRORLEVEL%"

if "%RC%"=="3" goto already
if not "%RC%"=="0" goto failed

echo.
echo 服务已停止。
pause
exit /b 0

:already
echo.
echo 已经有一个实例在运行，浏览器已打开对应页面。
timeout /t 3 >nul 2>nul
exit /b 0

:failed
echo.
echo 启动失败，退出码 %RC%。请把上面的提示截图反馈。
echo 常见原因：Python 未安装、ffmpeg 未配置、端口被占用。
pause
exit /b %RC%

:nopython
echo.
echo 没有找到可用的 Python。
echo 请安装 Python 3.9+ 并勾选 Add python.exe to PATH。
pause
exit /b 1
