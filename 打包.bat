@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title 打包 MergeStudio
cd /d "%~dp0"

rem ---------------------------------------------------------------
rem  打一个干净的发布包，直接发给别人用。
rem  - 只带运行必需的文件（代码 / 文档 / 启动脚本 / 测试）
rem  - 不会带上本机数据 .mergestudio（免得把你这台机器的 ffmpeg 路径
rem    和任务历史发出去），也不会带 __pycache__、.git、.venv
rem  - 用法：双击运行；或 打包.bat -q（跑完不停，方便脚本调用）
rem ---------------------------------------------------------------

set "NAME=MergeStudio"
set "SYS_TAR=%SystemRoot%\System32\tar.exe"
set "SIZE_MB="

echo [1/4] 准备临时目录 _dist ...
if exist "_dist" rd /s /q "_dist"
mkdir "_dist\%NAME%" 2>nul
if not exist "_dist\%NAME%\" goto failed

echo [2/4] 复制文件（不含本机数据与缓存）...
for %%I in (mergestudio tests docs) do (
  if exist "%%I" xcopy "%%I" "_dist\%NAME%\%%I\" /E /I /Y /Q >nul
)
for %%F in (run.py requirements.txt pyproject.toml README.md CHANGELOG.md LICENSE .gitignore start.sh 启动.bat 停止.bat) do (
  if exist "%%F" copy /y "%%F" "_dist\%NAME%\" >nul
)

echo [3/4] 清掉 __pycache__ 与 *.pyc ...
for /d /r "_dist\%NAME%" %%D in (__pycache__) do (
  if exist "%%D" rd /s /q "%%D"
)
del /s /q "_dist\%NAME%\*.pyc" >nul 2>nul

rem 版本号从 config.py 里取，取不到就用 dev，不影响使用
set "VER="
for /f "tokens=3" %%A in ('findstr /b /c:"APP_VERSION" "mergestudio\config.py" 2^>nul') do set "VER=%%A"
if defined VER set "VER=!VER:"=!"
if not defined VER set "VER=dev"
set "ZIP=%NAME%-v%VER%.zip"

echo [4/4] 压缩成 %ZIP% ...
if exist "%ZIP%" del /q "%ZIP%" 2>nul
set "PACKED="

rem 优先用 PowerShell 的 Compress-Archive：它给中文文件名打 UTF-8 标记，
rem 对方在英文系统、Mac、7-Zip 里解压都不会乱码。
if not defined PACKED (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path '_dist\%NAME%' -DestinationPath '%ZIP%' -Force" >nul 2>nul
  if exist "%ZIP%" set "PACKED=powershell"
)

rem 退路：系统自带的 tar.exe（bsdtar）。必须写全路径，PATH 里若有 Git/MSYS
rem 的 GNU tar，它不认 zip，会生成一个只是改了后缀的 tar 包。
if not defined PACKED (
  if exist "%SYS_TAR%" (
    "%SYS_TAR%" -a -c -f "%ZIP%" -C "_dist" "%NAME%" 2>nul && set "PACKED=tar"
  )
)

if not defined PACKED goto failed
if not exist "%ZIP%" goto failed

rem 真校验一次：能列出内容才算正常 zip
if exist "%SYS_TAR%" (
  "%SYS_TAR%" -tf "%ZIP%" >nul 2>nul || goto failed
)
for %%A in ("%ZIP%") do (
  if %%~zA LSS 20000 goto failed
  set "SIZE=%%~zA"
)
set /a SIZE_KB=!SIZE! / 1024
if !SIZE! GEQ 1048576 set /a SIZE_MB=!SIZE! / 1048576
rd /s /q "_dist"

echo.
echo ============================================================
if defined SIZE_MB (
  echo  打包完成：%ZIP%   （约 !SIZE_MB! MB，用 %PACKED% 压缩）
) else (
  echo  打包完成：%ZIP%   （约 !SIZE_KB! KB，用 %PACKED% 压缩）
)
echo ============================================================
echo.
echo  对方拿到后只需要三步：
echo    1) 装 Python 3.9+，安装时勾选 Add python.exe to PATH 和 tcl/tk
echo    2) 装 ffmpeg（解压即用，不用配环境变量）
echo    3) 双击「启动.bat」，在网页「设置」里选 ffmpeg.exe 并保存
echo.
echo  提醒：启动.bat 是 GBK 编码，别用编辑器另存成 UTF-8，否则中文乱码。
echo.
if /i not "%~1"=="-q" pause
exit /b 0

:failed
echo.
echo [失败] 打包没成功，zip 没能正常生成或校验不通过。
echo        临时目录 _dist 先留着，方便看是哪一步断的。
echo        常见原因：旧 zip 正被打开、磁盘空间不足、没有写权限。
if /i not "%~1"=="-q" pause
exit /b 1
