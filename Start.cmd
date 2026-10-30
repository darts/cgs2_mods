@echo off
setlocal
cd /d "%~dp0"
where mise >nul 2>nul
if errorlevel 1 (
    echo Install mise first: winget install jdx.mise
    echo Then follow the three commands in readme.md and reopen this window.
    pause
    exit /b 1
)
call mise run start
if errorlevel 1 (
    echo See readme.md for mise trust and setup.
    pause
    exit /b 1
)
