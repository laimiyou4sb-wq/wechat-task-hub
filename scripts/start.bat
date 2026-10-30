@echo off
setlocal
cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)

python -c "import fastapi, uvicorn, httpx, openai" >nul 2>nul
if errorlevel 1 (
    echo [1/2] Installing dependencies...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] pip install failed. Check your network or mirror settings.
        pause
        exit /b 1
    )
)

echo [2/2] Starting WeChat Task Hub...
python run.py %*
pause
