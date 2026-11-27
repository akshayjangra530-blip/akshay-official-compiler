@echo off
title Akshay Official Compiler
cd /d "%~dp0"

if exist "C:\mingw64\bin\g++.exe" (
    set "PATH=C:\mingw64\bin;%PATH%"
)

where python >nul 2>nul
if errorlevel 1 (
    echo Error: Python 3 is not installed or not in PATH.
    pause
    exit /b 1
)

where g++ >nul 2>nul
if errorlevel 1 (
    echo Error: gcc / g++ compiler not found in PATH.
    pause
    exit /b 1
)

echo Starting Akshay Official Compiler...
echo Keep this window open while using the IDE. Press Ctrl+C to stop.
python app.py --open
