@echo off
chcp 65001 >nul
cd /d %~dp0
title StrategioAI CRM

echo =============================================
echo  StrategioAI CRM
echo =============================================

:: Drep gamle CRM-prosesser pa port 8000
powershell -ExecutionPolicy Bypass -Command "Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }" >nul 2>nul
timeout /t 2 /nobreak >nul

echo Starter CRM pa http://localhost:8000
start http://localhost:8000
python main.py
