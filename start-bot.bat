@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "EXIT_CODE=0"
set "DO_SYNC=0"
if /i "%~1"=="--sync" set "DO_SYNC=1"
if not exist ".venv\Scripts\python.exe" set "DO_SYNC=1"

if not exist ".env" (
    echo ERROR: .env not found.
    echo Copy .env.example to .env ^(copy .env.example .env^) and set BOT_TOKEN and ALLOWED_USER_IDS ^(or PUBLIC_ACCESS=true^).
    set "EXIT_CODE=1"
)

if "%EXIT_CODE%"=="0" (
    where ffmpeg >nul 2>nul || (
        echo ERROR: ffmpeg not found on PATH. Install with: winget install Gyan.FFmpeg
        set "EXIT_CODE=1"
    )
)

if "%EXIT_CODE%"=="0" (
    where ffprobe >nul 2>nul || (
        echo ERROR: ffprobe not found on PATH. Install with: winget install Gyan.FFmpeg
        set "EXIT_CODE=1"
    )
)

if "%EXIT_CODE%"=="0" if "%DO_SYNC%"=="1" (
    where uv >nul 2>nul || (
        echo ERROR: uv not found on PATH. Install from https://docs.astral.sh/uv/
        set "EXIT_CODE=1"
    )
)

if "%EXIT_CODE%"=="0" if "%DO_SYNC%"=="1" (
    echo Syncing Python environment with uv...
    call uv sync --locked --no-dev || set "EXIT_CODE=1"
)

if "%EXIT_CODE%"=="0" if not exist "data" (mkdir "data" || set "EXIT_CODE=1")
if "%EXIT_CODE%"=="0" if not exist "tmp\jobs" (mkdir "tmp\jobs" || set "EXIT_CODE=1")

if "%EXIT_CODE%"=="0" (
    set "PYTHONUNBUFFERED=1"
    set "PYTHONDONTWRITEBYTECODE=1"
    ".venv\Scripts\python.exe" -c "from app.config import Settings; Settings().validate_for_startup()" || (
        echo ERROR: Fix the configuration errors above in .env and run again.
        set "EXIT_CODE=1"
    )
)

if "%EXIT_CODE%"=="0" (
    echo Starting Telegram Media Downloader bot. Press Ctrl+C to stop.
    ".venv\Scripts\python.exe" -m app.main
)
if "%EXIT_CODE%"=="0" set "EXIT_CODE=%errorlevel%"

set "_CMD=%CMDCMDLINE:"=%"
if not "%EXIT_CODE%"=="0" if defined _CMD if "%_CMD:~-1%"==" " pause
exit /b %EXIT_CODE%
