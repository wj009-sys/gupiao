@echo off
chcp 65001 >nul
cd /d "C:\Users\65004\Desktop\小白\股票投资"
set TUSHARE_TOKEN=TUSHARE_TOKEN_PLACEHOLDER

if not exist logs mkdir logs
echo [%date% %time%] Agent1 START >> logs\scheduler.log

:: Python数据采集
"C:\Users\65004\Desktop\小白\股票投资\venv\Scripts\python.exe" -X utf8 scripts\agent1-情报采集\fetch_all.py
echo [%date% %time%] Agent1 DONE >> logs\scheduler.log
