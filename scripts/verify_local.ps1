# 在本機驗收目錄執行。預設 C:\Users\user\sdt-verify。不需要把逐字稿送出這台電腦。
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
if (-not $env:SDT_VERIFY_DIR) {
    $env:SDT_VERIFY_DIR = "C:\Users\user\sdt-verify"
}
$env:PYTHONIOENCODING = "utf-8"
if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3 scripts\verify_local.py --dir $env:SDT_VERIFY_DIR
} else {
    python scripts\verify_local.py --dir $env:SDT_VERIFY_DIR
}
exit $LASTEXITCODE
