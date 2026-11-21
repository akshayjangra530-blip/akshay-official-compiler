@echo off
title Akshay Compiler Launcher
cd /d "%~dp0"

echo Starting C Code Runner server...
start "Akshay Compiler Server" cmd /k "python app.py"

timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8000"
exit