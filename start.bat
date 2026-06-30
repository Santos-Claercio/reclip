@echo off
cd /d "%~dp0"
py -3.14 -m pip install -q -r requirements.txt 2>nul
start "" /B py -3.14 app.py
echo App iniciada em http://127.0.0.1:8899
timeout /t 2 >nul
start http://127.0.0.1:8899
