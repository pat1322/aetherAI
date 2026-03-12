@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM  AetherAI Device Agent — Standalone EXE Builder
REM  Run this once to produce AetherAI_Agent.exe in the dist\ folder.
REM  Requires: pip install pyinstaller
REM ─────────────────────────────────────────────────────────────────────────────

echo [AetherAI] Installing build dependencies...
pip install pyinstaller pyautogui Pillow websockets pyperclip --quiet

echo [AetherAI] Building standalone executable...

pyinstaller ^
  --onefile ^
  --noconsole ^
  --name "AetherAI_Agent" ^
  --icon "icon.ico" ^
  --add-data "aether_config.ini;." ^
  --hidden-import "PIL._tkinter_finder" ^
  --hidden-import "pyautogui" ^
  --hidden-import "pyperclip" ^
  --hidden-import "websockets" ^
  --hidden-import "asyncio" ^
  --hidden-import "configparser" ^
  agent.py

IF EXIST dist\AetherAI_Agent.exe (
    echo.
    echo ✓ Build successful!
    echo   Output: dist\AetherAI_Agent.exe
    echo.
    echo   Distribute this exe + aether_config.ini together.
    echo   Users just double-click AetherAI_Agent.exe to connect.
) ELSE (
    echo.
    echo ✗ Build failed. Check output above for errors.
)

pause
