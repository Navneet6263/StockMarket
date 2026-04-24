@echo off
setlocal

set "ROOT_DIR=%~dp0"
if "%ROOT_DIR:~-1%"=="\" set "ROOT_DIR=%ROOT_DIR:~0,-1%"

set "VENV_PY=%ROOT_DIR%\.venv\Scripts\python.exe"
set "VENV_ACTIVATE=%ROOT_DIR%\.venv\Scripts\activate.bat"
set "API_ENV=%ROOT_DIR%\services\api\.env"

echo ==================================================
echo   Stock Predictor - Start All Services
echo ==================================================
echo Root: %ROOT_DIR%
echo.

if not exist "%VENV_PY%" (
    echo ERROR: Python virtual environment not found.
    echo.
    echo Run these commands first:
    echo   python -m venv .venv
    echo   .venv\Scripts\activate
    echo   pip install -r services\api\requirements.txt
    echo.
    pause
    exit /b 1
)

if not exist "%VENV_ACTIVATE%" (
    echo ERROR: activate.bat not found in .venv\Scripts
    echo.
    pause
    exit /b 1
)

if not exist "%ROOT_DIR%\node_modules" (
    echo ERROR: Node dependencies not found.
    echo.
    echo Run:
    echo   npm install
    echo.
    pause
    exit /b 1
)

if not exist "%API_ENV%" (
    echo WARNING: services\api\.env not found.
    echo Telegram, MongoDB, and custom API config may not work until you create it.
    echo.
)

echo Launching API server...
start "Stock API Server" cmd /k "cd /d ""%ROOT_DIR%"" && call "".venv\Scripts\activate.bat"" && python -m uvicorn app.main:app --reload --port 8000 --app-dir services/api"

timeout /t 3 /nobreak >nul

echo Launching WebSocket server...
start "Stock WebSocket Server" cmd /k "cd /d ""%ROOT_DIR%"" && npm --workspace services/ws run dev"

timeout /t 2 /nobreak >nul

echo Launching Web frontend...
start "Stock Web App" cmd /k "cd /d ""%ROOT_DIR%"" && npm --workspace apps/web run dev"

echo.
echo Services started in separate windows.
echo.
echo Main URLs:
echo   API Health:   http://localhost:8000/health
echo   Market Pulse: http://localhost:8000/market/context?symbol=NIFTY
echo   Telegram:     http://localhost:8000/telegram/status
echo   WebSocket:    ws://localhost:4001
echo   Frontend:     http://localhost:3000
echo.
echo Notes:
echo   - Wait 10 to 20 seconds for all services to become ready.
echo   - Use POST /telegram/test to verify Telegram delivery.
echo   - If the frontend looks blank, check the three service windows for errors.
echo.
echo This launcher can be closed; services will keep running in their own windows.
pause

endlocal
