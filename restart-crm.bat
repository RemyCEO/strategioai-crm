@echo off
echo Stopper gammel CRM-server...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do taskkill /F /PID %%a 2>nul
timeout /t 1 /nobreak >nul
echo Starter ny CRM-server...
cd /d C:\Users\Laptop\crm
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
