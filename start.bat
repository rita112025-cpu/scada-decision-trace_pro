@echo off
chcp 65001 >nul
rem 啟動本機服務（只綁 127.0.0.1，不需管理員權限）
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  python -m sdt serve %*
  goto :eof
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m sdt serve %*
  goto :eof
)
echo 找不到 Python 3。請先安裝 Python 3.10 以上（使用者層級安裝即可）。
pause
