@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe scripts\check_requirements.py >nul 2>nul
if errorlevel 1 (
 echo NeuroScope: Installing updated dependencies...
 .venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt
 if errorlevel 1 exit /b 1
 .venv\Scripts\python.exe scripts\check_requirements.py --stamp
 if errorlevel 1 exit /b 1
)
.venv\Scripts\python.exe run.py --open %*
