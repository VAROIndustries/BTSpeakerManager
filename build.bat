@echo off
echo === BT Speaker Manager — Build ===
echo.

echo Installing dependencies...
pip install -r requirements.txt pyinstaller
echo.

echo Building EXE...
pyinstaller --onefile --noconsole --name "BTSpeakerManager" bt_tray.py
echo.

if exist "dist\BTSpeakerManager.exe" (
    echo === Build successful! ===
    echo EXE: dist\BTSpeakerManager.exe
    echo.
    echo To install:
    echo   1. Copy dist\BTSpeakerManager.exe to a permanent location
    echo   2. Run it once — it will add itself to Windows startup
    echo   3. Right-click the tray icon to configure
) else (
    echo === Build FAILED ===
    echo Check the output above for errors.
)
echo.
pause
