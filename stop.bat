@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  python -m sdt stop %*
  goto :eof
)
py -3 -m sdt stop %*
