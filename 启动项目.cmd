@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0" || (
    echo Failed to open the project directory.
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    rem prompt_toolkit needs a real Windows console. START creates one when
    rem this batch file was launched by a host that redirected its handles.
    start "minicode" /wait ".venv\Scripts\python.exe" -m app --project "%CD%" %*
) else (
    where uv >nul 2>nul
    if errorlevel 1 (
        echo Python environment not found. Install uv and run "uv sync" first.
        pause
        popd
        exit /b 1
    )
    start "minicode" /wait uv run minicode --project "%CD%" %*
)

set "exit_code=%ERRORLEVEL%"
if not "%exit_code%"=="0" pause
popd
exit /b %exit_code%
