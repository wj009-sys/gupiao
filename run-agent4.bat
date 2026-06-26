@echo off
chcp 65001 >nul
cd /d "C:\Users\65004\Desktop\小白\股票投资"
set TUSHARE_TOKEN=TUSHARE_TOKEN_PLACEHOLDER

if not exist logs mkdir logs
echo [%date% %time%] Agent4 START >> logs\scheduler.log
"C:\Users\65004\Desktop\小白\股票投资\venv\Scripts\python.exe" -X utf8 scripts\agent4-复盘\review.py
echo [%date% %time%] Agent4 DONE >> logs\scheduler.log
