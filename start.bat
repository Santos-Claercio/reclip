@echo off
cd /d "%~dp0"
start "" /B python app.py
echo App iniciada em http://127.0.0.1:8899
timeout /t 2 >nul
start http://127.0.0.1:8899
